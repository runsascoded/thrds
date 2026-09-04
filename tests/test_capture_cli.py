"""Tests for the `thrds capture …` subgroup.

Capture-only sessions have no platform target — just a git-tracked doc dir
optionally mirrored to a gist. Covers:

- `capture init` writes `platform='capture'` into `thrds.yml`
- `capture init` with gist enabled: mocked `create_gist` / `align` boundary,
  gist_id ends up in state; no state commit (capture state is local-only)
- `capture init` stdin mode (no DOC_PATH): content from stdin, slug derived
  or via -s, session dir on stdout
- `capture push` autocommits the doc (state is git-excluded)
- `capture update`: replace doc from stdin, commit + push; no-op when unchanged
- Slim state: a capture `thrds.yml` holds only non-default fields
- `capture open` prints / opens the gist URL (or errors if no gist)
- Platform-mismatch guard: `capture <verb>` on a slack-inited session (and
  vice versa) errors with a clear message, no half-execution
- Init cross-platform reject: `capture init` on a slack-inited partial-init
  session dir refuses instead of overwriting
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from thrds import SessionState
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.state import STATE_PATH


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """CWD → tmp_path so state.json read/write is scoped to the test.

    Sets git author/committer env so `git commit` in mirror.py works without
    depending on system-wide `git config`. Stamps a slack-token env var too
    (some helpers construct a client eagerly; capture verbs don't need it,
    but leaving it unset would flake other paths).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.com')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.com')
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _write_doc(tmp: Path, name: str = 'notes.md', text: str = "=== a\n\nHello.\n") -> str:
    (tmp / name).write_text(text)
    return name


def _init_capture(in_tmp: Path, monkeypatch, doc_name: str = 'notes.md') -> Path:
    """Write the doc, run `thrds capture init --no-gist <doc>`, chdir to session dir."""
    _write_doc(in_tmp, doc_name)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'capture' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


def _init_slack(in_tmp: Path, monkeypatch, doc_name: str = 'notes.md') -> Path:
    """Same, but slack-inited — for the mismatch tests."""
    _write_doc(in_tmp, doc_name)
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'slck' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- capture init ---


def test_capture_init_no_gist_writes_platform_capture(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'capture' / 'notes'
    state = SessionState.load(session)
    assert state.platform == 'capture'
    assert state.doc_path == 'notes.md'
    assert state.gist_id is None
    assert state.channel_prefix is None
    assert state.session_id  # non-empty uuid


def test_capture_init_no_gist_creates_git_repo_with_initial_commit(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    session = in_tmp / 'capture' / 'notes'
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init notes']


def test_capture_init_reports_generic_hint_no_staging_pc(in_tmp):
    """`slack init` prints the staging PC name; `capture init` does not
    (no channel concept). Both print `Initialized session ...` + cd hint."""
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    lines = result.stderr.rstrip().splitlines()
    # Exact shape: "Initialized session <uuid> at <dir>", then "cd <path> to work on this doc".
    # No staging PC line, no gist line (--no-gist).
    assert len(lines) == 2
    assert lines[0].startswith("Initialized session ")
    assert " at " in lines[0]
    assert lines[1].startswith("cd ") and lines[1].endswith(" to work on this doc")


def test_capture_init_gist_flow_calls_create_gist_and_records_gist_id(in_tmp, monkeypatch):
    """Init without --no-gist under `capture`: mirror.create_gist / align, but
    — unlike `slack init` — NO state commit/push afterward: capture state is
    local-only, so the gist stays purely the doc's seed commit."""
    _write_doc(in_tmp)
    calls: list[tuple] = []

    from thrds import mirror as mirror_mod

    def fake_create_gist(session_dir, description, files):
        calls.append(('create_gist', str(session_dir), description, list(files)))
        return ('abc123', 'git@gist.github.com:abc123.git')

    def fake_align(session_dir, remote='g', branch='main'):
        calls.append(('align_to_remote', remote, branch))

    def fake_push(session_dir, remote='g', branch='main'):
        calls.append(('push', remote, branch))

    monkeypatch.setattr(mirror_mod, 'create_gist', fake_create_gist)
    monkeypatch.setattr(mirror_mod, 'align_to_remote', fake_align)
    monkeypatch.setattr(mirror_mod, 'push', fake_push)

    result = CliRunner().invoke(cli, ['capture', 'init', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)

    session_dir = in_tmp / 'capture' / 'notes'
    state = SessionState.load(session_dir)
    assert state.platform == 'capture'
    assert state.gist_id == 'abc123'

    assert [c[0] for c in calls] == ['create_gist', 'align_to_remote']
    assert calls[0] == ('create_gist', str(session_dir), 'thrds: notes', ['notes.md'])


def test_capture_init_with_gist_reports_gist_url_hint(in_tmp, monkeypatch):
    _write_doc(in_tmp)
    from thrds import mirror as mirror_mod
    monkeypatch.setattr(mirror_mod, 'create_gist',
                        lambda *a, **k: ('abc123', 'git@gist.github.com:abc123.git'))
    monkeypatch.setattr(mirror_mod, 'align_to_remote', lambda *a, **k: None)
    monkeypatch.setattr(mirror_mod, 'push', lambda *a, **k: None)

    result = CliRunner().invoke(cli, ['capture', 'init', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    lines = result.stderr.rstrip().splitlines()
    # Exact shape: init line, gist hint, cd hint.
    assert len(lines) == 3
    assert lines[0].startswith("Initialized session ")
    assert lines[1] == "Gist: https://gist.github.com/abc123"
    assert lines[2].startswith("cd ") and lines[2].endswith(" to work on this doc")


# --- capture push ---


def test_capture_push_autocommits_doc(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    # Modify the doc to give autocommit something new to snapshot.
    (session_dir / 'notes.md').write_text("=== a\n\nUpdated.\n")
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    # init commit + capture-push commit (newest-first).
    assert log == ['thrds: capture push', 'thrds: init notes']


def test_capture_push_prints_no_gist_hint_when_missing(in_tmp, monkeypatch):
    """--no-gist session → push commits locally + prints an explicit no-gist note."""
    session_dir = _init_capture(in_tmp, monkeypatch)
    (session_dir / 'notes.md').write_text("=== a\n\nUpdated.\n")
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 0
    assert result.stderr.rstrip().splitlines() == ['(no gist configured — commit only)']


# --- capture open ---


def test_capture_open_prints_gist_url_with_no_open_flag(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    # Fake a gist_id so `open` has something to point at (no-gist init leaves it None).
    state = SessionState.load(session_dir)
    state.gist_id = 'def456'
    state.save(session_dir)

    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.rstrip().splitlines() == [
        'Opening gist def456: https://gist.github.com/def456',
    ]


def test_capture_open_errors_when_no_gist(in_tmp, monkeypatch):
    _init_capture(in_tmp, monkeypatch)  # no-gist init → gist_id stays None
    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No gist recorded — session was init'd with --no-gist."
    )


# --- platform-mismatch guard ---


def test_slack_push_on_capture_session_errors_clearly(in_tmp, monkeypatch):
    """`slack push` inside a capture-inited session dir → clear platform-mismatch error."""
    _init_capture(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'capture'; "
        "use `thrds capture <verb>` instead of `thrds slack <verb>`."
    )


def test_capture_push_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    """Symmetric: `capture push` inside a slack-inited session → clear error."""
    _init_slack(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds capture <verb>`."
    )


def test_capture_open_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    """Symmetric for `open` too."""
    _init_slack(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds capture <verb>`."
    )


# --- cross-platform init ---


def test_init_across_platforms_no_longer_collides(in_tmp):
    """Per-platform session dirs mean one doc can hold a session per platform.

    These two inits used to fight over a single ``thrds/<slug>/``; they now
    land in ``slck/notes`` and ``capture/notes`` and never see each other.
    """
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'notes.md'])
    assert r1.exit_code == 0, (r1.output, r1.stderr)
    r2 = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert r2.exit_code == 0, (r2.output, r2.stderr)
    assert [
        SessionState.load(in_tmp / d / 'notes').platform
        for d in ('slck', 'capture')
    ] == ['slack', 'capture']


def test_init_rejects_a_dir_inited_for_another_platform(in_tmp):
    """The platform guard now only fires on a hand-moved dir — so it must read
    the *stamped* platform, not infer one from the path it was found at."""
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert r1.exit_code == 0, (r1.output, r1.stderr)
    (in_tmp / 'slck').mkdir()
    (in_tmp / 'capture' / 'notes').rename(in_tmp / 'slck' / 'notes')

    r2 = CliRunner().invoke(cli, ['slack', 'init', 'notes.md'])
    assert r2.exit_code == 2
    assert r2.stderr.splitlines()[-1] == (
        "Error: Target session dir was inited for platform 'capture'; "
        "use `thrds capture init` (or delete the dir to reset)."
    )

# --- stdin init + `update` (the two-command flow) ---


def _gist_mocks(monkeypatch):
    """Stub the gist boundary: create_gist returns a fixed id; align/push no-op."""
    from thrds import mirror as mirror_mod
    monkeypatch.setattr(mirror_mod, 'create_gist',
                        lambda *a, **k: ('abc123', 'git@gist.github.com:abc123.git'))
    monkeypatch.setattr(mirror_mod, 'align_to_remote', lambda *a, **k: None)
    monkeypatch.setattr(mirror_mod, 'push', lambda *a, **k: None)


def test_capture_init_stdin_derives_slug_and_prints_dir(in_tmp, monkeypatch):
    _gist_mocks(monkeypatch)
    content = "**My Draft** Title\n\nbody text\n"
    result = CliRunner().invoke(cli, ['capture', 'init'], input=content)
    assert result.exit_code == 0, (result.output, result.stderr)
    session_dir = in_tmp / 'capture' / 'my-draft-title'
    assert result.stdout == f"{session_dir}\n"
    assert (session_dir / 'my-draft-title.md').read_text() == content
    state = SessionState.load(session_dir)
    assert state.platform == 'capture'
    assert state.doc_path == 'my-draft-title.md'
    assert state.gist_id == 'abc123'


def test_capture_init_stdin_slug_flag(in_tmp, monkeypatch):
    _gist_mocks(monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'init', '-s', 'custom-name'], input='body\n')
    assert result.exit_code == 0, (result.output, result.stderr)
    session_dir = in_tmp / 'capture' / 'custom-name'
    assert result.stdout == f"{session_dir}\n"
    assert (session_dir / 'custom-name.md').read_text() == 'body\n'


def test_capture_init_stdin_empty_content_requires_slug(in_tmp):
    result = CliRunner().invoke(cli, ['capture', 'init'], input='')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: Cannot derive a slug from the content's first line; pass -s/--slug."
    )


def test_capture_init_doc_path_prints_dir_on_stdout(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stdout == f"{in_tmp / 'capture' / 'notes'}\n"


def test_capture_state_untracked_in_new_sessions(in_tmp, monkeypatch):
    """thrds.yml is git-excluded: not tracked, and not "untracked noise" either."""
    session_dir = _init_capture(in_tmp, monkeypatch)
    tracked = subprocess.run(
        ['git', 'ls-files'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert tracked == ['notes.md']
    status = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout
    assert status == ''


def test_capture_yml_is_slim(in_tmp, monkeypatch):
    """A capture session's thrds.yml holds only non-default fields."""
    session_dir = _init_capture(in_tmp, monkeypatch)
    data = yaml.safe_load((session_dir / 'thrds.yml').read_text())
    assert data.pop('session_id')  # non-empty uuid
    assert data == {'doc_path': 'notes.md', 'platform': 'capture'}


def test_capture_update_replaces_doc_and_commits(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    monkeypatch.chdir(in_tmp)  # exercise the SESSION_DIR arg, not CWD
    result = CliRunner().invoke(
        cli, ['capture', 'update', str(session_dir)], input='v2 content\n',
    )
    assert result.exit_code == 0, (result.output, result.stderr)
    assert (session_dir / 'notes.md').read_text() == 'v2 content\n'
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: capture update', 'thrds: init notes']
    assert result.stderr.rstrip().splitlines() == ['(no gist configured — commit only)']


def test_capture_update_unchanged_is_noop(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    original = (session_dir / 'notes.md').read_text()
    result = CliRunner().invoke(cli, ['capture', 'update'], input=original)
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.rstrip().splitlines() == ['(no changes — nothing to push)']
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init notes']


def test_capture_update_defaults_to_cwd(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)  # chdirs into session_dir
    result = CliRunner().invoke(cli, ['capture', 'update'], input='v2\n')
    assert result.exit_code == 0, (result.output, result.stderr)
    assert (session_dir / 'notes.md').read_text() == 'v2\n'


def test_capture_update_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    _init_slack(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'update'], input='v2\n')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds capture <verb>`."
    )


def test_capture_update_outside_session_errors(in_tmp):
    result = CliRunner().invoke(cli, ['capture', 'update'], input='v2\n')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: No thrds session state at {in_tmp / 'thrds.yml'}; "
        "run `thrds <platform> init` first."
    )


def test_capture_two_command_flow(in_tmp, monkeypatch):
    """The acceptance flow from the spec: init from stdin, update by dir, no-op re-update."""
    _gist_mocks(monkeypatch)
    r1 = CliRunner().invoke(cli, ['capture', 'init'], input='draft v1\n')
    assert r1.exit_code == 0, (r1.output, r1.stderr)
    session_dir = Path(r1.stdout.rstrip('\n'))
    assert session_dir == in_tmp / 'capture' / 'draft-v1'
    r2 = CliRunner().invoke(cli, ['capture', 'update', str(session_dir)], input='final v2\n')
    assert r2.exit_code == 0, (r2.output, r2.stderr)
    assert (session_dir / 'draft-v1.md').read_text() == 'final v2\n'
    r3 = CliRunner().invoke(cli, ['capture', 'update', str(session_dir)], input='final v2\n')
    assert r3.exit_code == 0
    assert r3.stderr.rstrip().splitlines() == ['(no changes — nothing to push)']
