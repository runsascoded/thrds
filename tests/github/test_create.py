"""Tests for create command with mocked gh API calls."""

import subprocess
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from tempfile import TemporaryDirectory
import os
from click.testing import CliRunner

from thrds.platforms.github.cli import cli
from thrds.platforms.github.commands.create import (
    create_new_pr,
    create_new_issue,
    _resolve_draft_path,
    _ensure_nested_git_repo,
)


def _run(*args, cwd=None):
    """Run a command and assert success."""
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _toplevel(cwd) -> str | None:
    """Return `git rev-parse --show-toplevel` (resolved), or None if not a repo."""
    try:
        out = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.strip()
        return str(Path(out).resolve())
    except subprocess.CalledProcessError:
        return None


class TestResolveDraftPath:
    """Unit tests for _resolve_draft_path."""

    def test_default_is_gh_new(self):
        assert _resolve_draft_path(None) == Path('gh/new')

    def test_empty_string_is_default(self):
        assert _resolve_draft_path('') == Path('gh/new')

    def test_bare_slug_resolves_to_gh_drafts_slug(self):
        assert _resolve_draft_path('foo') == Path('gh/drafts/foo')
        assert _resolve_draft_path('my-feature') == Path('gh/drafts/my-feature')

    def test_path_with_slash_is_used_as_is(self):
        assert _resolve_draft_path('gh/drafts/foo') == Path('gh/drafts/foo')
        assert _resolve_draft_path('elsewhere/foo') == Path('elsewhere/foo')
        assert _resolve_draft_path('/abs/path') == Path('/abs/path')


class TestEnsureNestedGitRepo:
    """Tests for _ensure_nested_git_repo auto-init behavior."""

    def test_inits_nested_repo_when_parent_owns_cwd(self, tmp_path):
        """When the parent git repo owns cwd, init a nested repo here."""
        # Parent repo with gh/ ignored
        _run('git', 'init', '-q', cwd=tmp_path)
        (tmp_path / '.gitignore').write_text('gh/\n')
        draft_dir = tmp_path / 'gh' / 'drafts' / 'foo'
        draft_dir.mkdir(parents=True)

        # Sanity: from draft_dir, git toplevel is the parent
        assert _toplevel(draft_dir) == str(tmp_path.resolve())

        old_cwd = os.getcwd()
        try:
            os.chdir(draft_dir)
            _ensure_nested_git_repo('o', 'r', '42', 'https://x/42', 'issue')
        finally:
            os.chdir(old_cwd)

        # Now draft_dir is its own toplevel
        assert _toplevel(draft_dir) == str(draft_dir.resolve())
        # Metadata was written to nested git config
        assert (draft_dir / '.git').is_dir()
        cfg = subprocess.run(
            ['git', 'config', '--get', 'pr.number'],
            cwd=draft_dir, capture_output=True, text=True,
        ).stdout.strip()
        assert cfg == '42'

    def test_noop_when_cwd_is_already_nested_toplevel(self, tmp_path):
        """If cwd is already its own toplevel, don't reinit."""
        _run('git', 'init', '-q', cwd=tmp_path)
        nested = tmp_path / 'nested'
        nested.mkdir()
        _run('git', 'init', '-q', cwd=nested)
        # Mark this repo with a sentinel so we can detect a re-init
        _run('git', 'config', 'sentinel.value', 'preserved', cwd=nested)

        old_cwd = os.getcwd()
        try:
            os.chdir(nested)
            _ensure_nested_git_repo('o', 'r', '7', 'https://x/7', 'issue')
        finally:
            os.chdir(old_cwd)

        # Sentinel survived (no reinit clobbered the config)
        cfg = subprocess.run(
            ['git', 'config', '--get', 'sentinel.value'],
            cwd=nested, capture_output=True, text=True,
        ).stdout.strip()
        assert cfg == 'preserved'
        # And the pr.* config from _ensure_nested_git_repo wasn't written
        # (we only init metadata when we actually init a new repo)
        result = subprocess.run(
            ['git', 'config', '--get', 'pr.number'],
            cwd=nested, capture_output=True, text=True,
        )
        assert result.returncode != 0, "pr.number should not have been set"

    def test_inits_when_no_git_repo_at_all(self, tmp_path):
        """If cwd isn't inside any git repo, init one here."""
        draft_dir = tmp_path / 'orphan'
        draft_dir.mkdir()
        assert _toplevel(draft_dir) is None

        old_cwd = os.getcwd()
        try:
            os.chdir(draft_dir)
            _ensure_nested_git_repo('o', 'r', '99', 'https://x/99', 'pr')
        finally:
            os.chdir(old_cwd)

        assert _toplevel(draft_dir) == str(draft_dir.resolve())


class TestCreateWithGitignoredGh:
    """End-to-end: create from a draft when parent repo has `gh/` ignored.

    This is the failure mode that `_ensure_nested_git_repo` was added to
    fix: without it, `git rm DESCRIPTION.md` fails because the file
    isn't tracked in the parent repo (it's gitignored).
    """

    def test_create_issue_succeeds_with_gh_gitignored(self, tmp_path, monkeypatch):
        # Parent repo with gh/ ignored
        _run('git', 'init', '-q', cwd=tmp_path)
        _run('git', 'config', 'user.email', 't@example.com', cwd=tmp_path)
        _run('git', 'config', 'user.name', 'T', cwd=tmp_path)
        (tmp_path / '.gitignore').write_text('gh/\n')

        # User has a manually-created draft (no `ghpr init`)
        draft_dir = tmp_path / 'gh' / 'drafts' / 'test-issue'
        draft_dir.mkdir(parents=True)
        (draft_dir / 'DESCRIPTION.md').write_text('# Test Issue\n\nBody\n')

        monkeypatch.chdir(draft_dir)
        # Identity for the nested repo `_ensure_nested_git_repo` will create
        # (CI runners have no global git config)
        for var in ('GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL'):
            monkeypatch.setenv(var, 't@example.com')
        for var in ('GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME'):
            monkeypatch.setenv(var, 'T')

        from thrds.platforms.github.commands import create as create_mod

        def fake_text(*args, **kwargs):
            if args[:3] == ('gh', 'issue', 'create'):
                return 'https://github.com/owner/repo/issues/42\n'
            raise AssertionError(f'unexpected proc.text call: {args}')

        with patch.object(create_mod.proc, 'text', side_effect=fake_text), \
             patch.object(create_mod, 'get_owner_repo', return_value=('owner', 'repo')), \
             patch('thrds.platforms.github.commands.push.push'):
            create_mod.create_new_issue(repo_arg=None, yes=2, dry_run=False)

        # Draft dir was renamed to gh/42/
        assert not draft_dir.exists()
        final = tmp_path / 'gh' / '42'
        assert final.is_dir()
        assert (final / 'repo#42.md').exists()

        # And the nested git repo has the file committed
        assert (final / '.git').is_dir()
        log = subprocess.run(
            ['git', 'log', '--oneline'],
            cwd=final, check=True, capture_output=True, text=True,
        ).stdout
        assert 'repo#42.md' in log

    def test_create_issue_succeeds_with_existing_nested_repo(self, tmp_path, monkeypatch):
        """Regression: existing nested git repo (from `ghpr init`) still works.

        Verifies the `git rm --ignore-unmatch` change didn't break the
        case where DESCRIPTION.md was already tracked in the nested repo.
        """
        # Parent repo with gh/ ignored
        _run('git', 'init', '-q', cwd=tmp_path)
        (tmp_path / '.gitignore').write_text('gh/\n')

        # Draft with its OWN nested git repo and a committed DESCRIPTION.md
        # (simulating what `ghpr init` would have produced)
        draft_dir = tmp_path / 'gh' / 'drafts' / 'test-issue'
        draft_dir.mkdir(parents=True)
        (draft_dir / 'DESCRIPTION.md').write_text('# Test Issue\n\nBody\n')
        _run('git', 'init', '-q', cwd=draft_dir)
        _run('git', 'config', 'user.email', 't@example.com', cwd=draft_dir)
        _run('git', 'config', 'user.name', 'T', cwd=draft_dir)
        _run('git', 'add', 'DESCRIPTION.md', cwd=draft_dir)
        _run('git', 'commit', '-q', '-m', 'init', cwd=draft_dir)

        monkeypatch.chdir(draft_dir)

        from thrds.platforms.github.commands import create as create_mod

        def fake_text(*args, **kwargs):
            if args[:3] == ('gh', 'issue', 'create'):
                return 'https://github.com/owner/repo/issues/7\n'
            raise AssertionError(f'unexpected proc.text call: {args}')

        with patch.object(create_mod.proc, 'text', side_effect=fake_text), \
             patch.object(create_mod, 'get_owner_repo', return_value=('owner', 'repo')), \
             patch('thrds.platforms.github.commands.push.push'):
            create_mod.create_new_issue(repo_arg=None, yes=2, dry_run=False)

        # Renamed dir and committed file
        final = tmp_path / 'gh' / '7'
        assert (final / 'repo#7.md').exists()
        assert not (final / 'DESCRIPTION.md').exists()

        # The commit that finalized the rename should show DESCRIPTION.md
        # deleted and repo#7.md added (i.e. the deletion was properly staged).
        diff = subprocess.run(
            ['git', 'log', '-1', '--name-status', '--format='],
            cwd=final, check=True, capture_output=True, text=True,
        ).stdout.strip().split('\n')
        # Either two separate D + A lines or a rename R line; check both files appear
        assert any('DESCRIPTION.md' in line for line in diff), diff
        assert any('repo#7.md' in line for line in diff), diff


class TestInitSlugMode:
    """Test init with slug-based path argument for parallel drafts."""

    def test_init_with_slug_creates_gh_drafts_slug(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch('thrds.platforms.github.commands.create.proc'):
                result = runner.invoke(cli, ['init', '-r', 'o/r', 'foo'])
                assert result.exit_code == 0, result.output
                assert Path('gh/drafts/foo').is_dir()
                assert Path('gh/drafts/foo/DESCRIPTION.md').exists()
                assert not Path('gh/new').exists()
                assert 'GHPR_DIR:gh/drafts/foo' in result.stdout

    def test_init_two_drafts_in_parallel(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch('thrds.platforms.github.commands.create.proc'):
                r1 = runner.invoke(cli, ['init', '-r', 'o/r', 'foo'])
                r2 = runner.invoke(cli, ['init', '-r', 'o/r', 'bar'])
                assert r1.exit_code == 0
                assert r2.exit_code == 0
                assert Path('gh/drafts/foo/DESCRIPTION.md').exists()
                assert Path('gh/drafts/bar/DESCRIPTION.md').exists()

    def test_init_default_prints_ghpr_dir_marker(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch('thrds.platforms.github.commands.create.proc'):
                result = runner.invoke(cli, ['init', '-r', 'o/r'])
                assert result.exit_code == 0
                assert 'GHPR_DIR:gh/new' in result.stdout

    def test_init_slug_collision_errors_out(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path('gh/drafts/foo').mkdir(parents=True)
            Path('gh/drafts/foo/DESCRIPTION.md').write_text('# existing\n')
            with patch('thrds.platforms.github.commands.create.proc'):
                result = runner.invoke(cli, ['init', '-r', 'o/r', 'foo'])
                assert result.exit_code != 0


class TestInit:
    """Test init command."""

    def test_init_with_explicit_repo(self, tmp_path):
        """Test init with -r owner/repo flag."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch('thrds.platforms.github.commands.create.proc') as mock_proc:
                result = runner.invoke(cli, ['init', '-r', 'test-owner/test-repo'])

                assert result.exit_code == 0

                # Extract all git commands called
                git_commands = [
                    call[0][0] if call[0] else None
                    for call in mock_proc.run.call_args_list
                ]
                assert 'git' in git_commands

                # Find specific git config calls
                git_config_calls = [
                    call[0] for call in mock_proc.run.call_args_list
                    if len(call[0]) > 2 and call[0][0] == 'git' and call[0][1] == 'config'
                ]
                assert ('git', 'config', 'pr.owner', 'test-owner') in git_config_calls
                assert ('git', 'config', 'pr.repo', 'test-repo') in git_config_calls

                # Check gh/new/ directory and DESCRIPTION.md were created
                assert Path('gh/new').is_dir()
                assert Path('gh/new/DESCRIPTION.md').exists()

    def test_init_already_initialized(self, tmp_path):
        """Test init fails when gh/new/DESCRIPTION.md exists."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path('gh/new').mkdir(parents=True)
            Path('gh/new/DESCRIPTION.md').write_text('# Test\n')

            result = runner.invoke(cli, ['init'])
            assert result.exit_code != 0


class TestCreateIssue:
    """Test issue creation with mocked gh calls."""

    def test_create_issue_dry_run(self, tmp_path):
        """Test issue creation in dry-run mode."""
        os.chdir(tmp_path)
        Path('DESCRIPTION.md').write_text('# Test Issue\n\nTest body content\n')
        Path('.git').mkdir()

        with patch('thrds.platforms.github.commands.create.proc') as mock_proc, \
             patch('thrds.platforms.github.commands.create.get_owner_repo') as mock_get_repo, \
             patch('thrds.platforms.github.commands.create.err'):

            mock_get_repo.return_value = ('test-owner', 'test-repo')

            # Should not make any gh calls in dry-run mode
            create_new_issue(repo_arg=None, yes=2, dry_run=True)

            # Verify no gh issue create call was made
            gh_calls = [call for call in mock_proc.text.call_args_list if 'gh' in str(call)]
            assert len(gh_calls) == 0

    def test_create_issue_success(self, tmp_path):
        """Test successful issue creation."""
        os.chdir(tmp_path)
        Path('DESCRIPTION.md').write_text('# Test Issue\n\nTest body content\n')
        Path('.git').mkdir()

        with patch('thrds.platforms.github.commands.create.proc') as mock_proc, \
             patch('thrds.platforms.github.commands.create.get_owner_repo') as mock_get_repo, \
             patch('thrds.platforms.github.commands.create.read_description_file') as mock_read_desc, \
             patch('thrds.platforms.github.commands.create.write_description_with_link_ref') as mock_write, \
             patch('thrds.platforms.github.commands.push.push') as mock_push, \
             patch('thrds.platforms.github.commands.create.err'), \
             patch('os.rename'):

            mock_get_repo.return_value = ('test-owner', 'test-repo')
            mock_proc.text.return_value = 'https://github.com/test-owner/test-repo/issues/42'
            mock_read_desc.return_value = ('Test Issue', 'Test body content')

            create_new_issue(repo_arg=None, yes=2, dry_run=False)

            # Verify gh issue create was called with exact arguments
            mock_proc.text.assert_called_once()
            call_args = mock_proc.text.call_args[0]
            expected_args = ('gh', 'issue', 'create', '-R', 'test-owner/test-repo', '--title', 'Test Issue', '--body', 'Test body content')
            assert call_args == expected_args

            # Verify git config was set with exact calls
            config_calls = [
                call[0] for call in mock_proc.run.call_args_list
                if len(call[0]) > 1 and call[0][0] == 'git' and call[0][1] == 'config'
            ]
            assert ('git', 'config', 'pr.number', '42') in config_calls
            assert ('git', 'config', 'pr.type', 'issue') in config_calls

            # Verify file was written with link reference
            mock_write.assert_called_once()
            write_args = mock_write.call_args[0]
            assert write_args[1] == 'test-owner'
            assert write_args[2] == 'test-repo'
            assert write_args[3] == '42'

    def test_create_issue_with_explicit_repo(self, tmp_path):
        """Test issue creation with -r flag."""
        os.chdir(tmp_path)
        Path('DESCRIPTION.md').write_text('# Test Issue\n\nTest body\n')
        Path('.git').mkdir()

        with patch('thrds.platforms.github.commands.create.proc') as mock_proc, \
             patch('thrds.platforms.github.commands.create.get_owner_repo') as mock_get_repo, \
             patch('thrds.platforms.github.commands.create.read_description_file') as mock_read_desc, \
             patch('thrds.platforms.github.commands.create.write_description_with_link_ref'), \
             patch('thrds.platforms.github.commands.push.push'), \
             patch('thrds.platforms.github.commands.create.err'), \
             patch('os.rename'):

            mock_get_repo.return_value = ('other-owner', 'other-repo')
            mock_proc.text.return_value = 'https://github.com/other-owner/other-repo/issues/123'
            mock_read_desc.return_value = ('Test Issue', 'Test body')

            create_new_issue(repo_arg='other-owner/other-repo', yes=2, dry_run=False)

            # Verify get_owner_repo was called with the arg
            mock_get_repo.assert_called_once_with('other-owner/other-repo')


class TestCreatePR:
    """Test PR creation with mocked gh calls.

    Note: PR creation has complex branch detection logic that makes
    comprehensive mocking difficult. These tests focus on the core
    gh API call behavior. Full integration testing is done manually
    or with real repos.
    """

    def test_create_pr_with_explicit_args(self, tmp_path):
        """Test PR creation with explicit head/base arguments."""
        os.chdir(tmp_path)
        Path('DESCRIPTION.md').write_text('# Test PR\n\nTest body\n')
        Path('.git').mkdir()

        with patch('thrds.platforms.github.commands.create.proc') as mock_proc, \
             patch('thrds.platforms.github.commands.create.read_description_file') as mock_read_desc, \
             patch('thrds.platforms.github.commands.create.write_description_with_link_ref') as mock_write, \
             patch('thrds.platforms.github.commands.push.push'), \
             patch('thrds.platforms.github.commands.create.err'), \
             patch('os.rename'):

            # Setup mocks for successful flow
            def line_side_effect(*args, **kwargs):
                if 'pr.owner' in args:
                    return 'test-owner'
                elif 'pr.repo' in args:
                    return 'test-repo'
                return ''

            mock_proc.line.side_effect = line_side_effect
            mock_proc.text.return_value = 'https://github.com/test-owner/test-repo/pull/42'
            mock_proc.lines.return_value = []
            mock_read_desc.return_value = ('Test PR', 'Test body')

            # Provide explicit head and base to avoid branch detection
            create_new_pr(
                head='feature-branch',
                base='main',
                draft=False,
                repo_arg=None,
                yes=2,
                dry_run=False
            )

            # Verify gh pr create was called with exact arguments
            mock_proc.text.assert_called_once()
            call_args = mock_proc.text.call_args[0]
            expected_args = (
                'gh', 'pr', 'create',
                '-R', 'test-owner/test-repo',
                '--title', 'Test PR',
                '--body', 'Test body',
                '--base', 'main',
                '--head', 'feature-branch'
            )
            assert call_args == expected_args

    def test_create_draft_pr(self, tmp_path):
        """Test draft PR includes --draft flag."""
        os.chdir(tmp_path)
        Path('DESCRIPTION.md').write_text('# Draft PR\n\nDraft body\n')
        Path('.git').mkdir()

        with patch('thrds.platforms.github.commands.create.proc') as mock_proc, \
             patch('thrds.platforms.github.commands.create.read_description_file') as mock_read_desc, \
             patch('thrds.platforms.github.commands.create.write_description_with_link_ref'), \
             patch('thrds.platforms.github.commands.push.push'), \
             patch('thrds.platforms.github.commands.create.err'), \
             patch('os.rename'):

            def line_side_effect(*args, **kwargs):
                if 'pr.owner' in args:
                    return 'test-owner'
                elif 'pr.repo' in args:
                    return 'test-repo'
                return ''

            mock_proc.line.side_effect = line_side_effect
            mock_proc.text.return_value = 'https://github.com/test-owner/test-repo/pull/99'
            mock_proc.lines.return_value = []
            mock_read_desc.return_value = ('Draft PR', 'Draft body')

            create_new_pr(
                head='feature',
                base='main',
                draft=True,  # Draft flag
                repo_arg=None,
                yes=2,  # Create silently (skip all prompts)
                dry_run=False
            )

            # Verify --draft flag was passed with exact arguments
            call_args = mock_proc.text.call_args[0]
            expected_args = (
                'gh', 'pr', 'create',
                '-R', 'test-owner/test-repo',
                '--title', 'Draft PR',
                '--body', 'Draft body',
                '--base', 'main',
                '--head', 'feature',
                '--draft'
            )
            assert call_args == expected_args
