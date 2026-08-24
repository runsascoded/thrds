"""End-to-end tests for ghpr clone.

These tests require `gh` CLI authentication and network access.
Skip with: pytest -m 'not e2e'
"""

import os
import subprocess
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory

import pytest


def gh_authenticated() -> bool:
    """Check if `gh` CLI is installed and authenticated."""
    if not which('gh'):
        return False
    try:
        result = subprocess.run(
            ['gh', 'auth', 'status'],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


requires_gh = pytest.mark.skipif(
    not gh_authenticated(),
    reason='requires authenticated gh CLI',
)
e2e = pytest.mark.e2e


@requires_gh
@e2e
class TestCloneE2E:
    """End-to-end clone tests using real GitHub PRs."""

    def test_clone_pr_no_gist(self):
        """Clone a known PR with --no-gist, verify directory structure."""
        with TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    'ghpr', 'clone',
                    'https://github.com/runsascoded/ghpr/pull/6',
                    '--no-gist',
                    '-d', os.path.join(tmpdir, 'pr6'),
                ],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"clone failed: {result.stderr}"

            pr_dir = Path(tmpdir) / 'pr6'
            assert pr_dir.is_dir()

            # Should have a description file
            desc_files = list(pr_dir.glob('*.md'))
            assert len(desc_files) >= 1, f"No .md files in {pr_dir}"

            # Description file should be repo#number.md
            desc_names = [f.name for f in desc_files if not f.name.startswith('z')]
            assert 'ghpr#6.md' in desc_names

            # Should have git config set
            owner = subprocess.run(
                ['git', 'config', 'pr.owner'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout.strip()
            assert owner == 'runsascoded'

            repo = subprocess.run(
                ['git', 'config', 'pr.repo'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout.strip()
            assert repo == 'ghpr'

            pr_type = subprocess.run(
                ['git', 'config', 'pr.type'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout.strip()
            assert pr_type == 'pr'

    def test_clone_pr_with_gist_reuse(self):
        """Clone a PR that has an existing gist footer; verify gist is reused."""
        with TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    'ghpr', 'clone',
                    'https://github.com/marin-community/marin/pull/1723',
                    '--no-comments',
                    '-d', os.path.join(tmpdir, 'pr1723'),
                ],
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, f"clone failed: {result.stderr}"

            pr_dir = Path(tmpdir) / 'pr1723'
            assert pr_dir.is_dir()

            # Should have detected and reused existing gist
            assert 'Found existing gist' in result.stderr, (
                f"Expected gist reuse message in stderr:\n{result.stderr}"
            )

            # Gist remote should be configured
            gist_remote = subprocess.run(
                ['git', 'config', 'pr.gist'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout.strip()
            assert gist_remote, "pr.gist config not set (gist not reused)"

            # Gist should be added as a git remote
            remotes = subprocess.run(
                ['git', 'remote', '-v'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout
            assert 'gist.github.com' in remotes

    def test_clone_issue_no_gist(self):
        """Clone a known issue, verify it's detected as an issue."""
        with TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    'ghpr', 'clone',
                    'https://github.com/marin-community/marin/issues/1773',
                    '--no-gist', '--no-comments',
                    '-d', os.path.join(tmpdir, 'issue1773'),
                ],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"clone failed: {result.stderr}"

            pr_dir = Path(tmpdir) / 'issue1773'
            assert pr_dir.is_dir()

            pr_type = subprocess.run(
                ['git', 'config', 'pr.type'],
                capture_output=True, text=True, cwd=pr_dir,
            ).stdout.strip()
            assert pr_type == 'issue'
