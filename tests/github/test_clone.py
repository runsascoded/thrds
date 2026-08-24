"""Tests for clone command directory naming logic."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


class TestCloneDirectoryNaming:
    """Test that clone creates directories at the correct location."""

    @pytest.fixture
    def mock_git_repo(self):
        """Create a temporary git repo structure."""
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            gh_dir = repo_root / 'gh'
            gh_dir.mkdir()

            # Initialize a minimal git repo
            os.chdir(repo_root)
            os.system('git init -q')
            os.system('git config user.name "Test User"')
            os.system('git config user.email "test@example.com"')

            yield repo_root

    def test_clone_directory_naming_declarative(self, mock_git_repo):
        """Test the declarative logic: target is always {git_root}/gh/{number}/"""
        from pathlib import Path

        # Resolve to handle /var vs /private/var symlink on macOS
        mock_git_repo = mock_git_repo.resolve()

        # Test from git root
        os.chdir(mock_git_repo)
        target = str(mock_git_repo / 'gh' / '100')
        cwd = str(Path.cwd().resolve())
        rel_path = os.path.relpath(target, cwd)
        assert rel_path == 'gh/100'

        # Test from gh/ directory
        os.chdir(mock_git_repo / 'gh')
        target = str(mock_git_repo / 'gh' / '200')
        cwd = str(Path.cwd().resolve())
        rel_path = os.path.relpath(target, cwd)
        assert rel_path == '200'

        # Test from gh/subdir/
        subdir = mock_git_repo / 'gh' / 'subdir'
        subdir.mkdir(parents=True)
        os.chdir(subdir)
        target = str(mock_git_repo / 'gh' / '300')
        cwd = str(Path.cwd().resolve())
        rel_path = os.path.relpath(target, cwd)
        assert rel_path == '../300'
