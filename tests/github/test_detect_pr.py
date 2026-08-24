"""Tests for _detect_current_branch_pr."""

from unittest.mock import patch, MagicMock

import pytest

from thrds.platforms.github.commands.clone import _detect_current_branch_pr


class TestDetectCurrentBranchPR:
    """Unit tests for detecting the current branch's PR."""

    def test_detects_pr_from_gh_pr_view(self):
        """When `gh pr view` returns valid data, extract owner/repo/number."""
        mock_data = {
            'number': 100,
            'url': 'https://github.com/Quantum-Accelerators/electrai/pull/100',
        }
        with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
            mock_proc.json.return_value = mock_data
            owner, repo, number, item_type = _detect_current_branch_pr()
        assert owner == 'Quantum-Accelerators'
        assert repo == 'electrai'
        assert number == '100'
        assert item_type == 'pr'

    def test_fallback_to_repo_view_when_url_missing(self):
        """When URL doesn't match pattern, fall back to `gh repo view`."""
        pr_data = {'number': 42, 'url': ''}
        repo_data = {'owner': {'login': 'someorg'}, 'name': 'somerepo'}
        with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
            mock_proc.json.side_effect = [pr_data, repo_data]
            owner, repo, number, item_type = _detect_current_branch_pr()
        assert owner == 'someorg'
        assert repo == 'somerepo'
        assert number == '42'
        assert item_type == 'pr'

    def test_returns_none_when_no_pr(self):
        """When `gh pr view` fails (no PR for branch), return None tuple."""
        with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
            mock_proc.json.side_effect = Exception('no PR found')
            result = _detect_current_branch_pr()
        assert result == (None, None, None, None)

    def test_returns_none_when_empty_response(self):
        """When `gh pr view` returns None/empty, return None tuple."""
        with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
            mock_proc.json.return_value = None
            result = _detect_current_branch_pr()
        assert result == (None, None, None, None)

    def test_returns_none_when_number_missing(self):
        """When response lacks a number field, return None tuple."""
        with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
            mock_proc.json.return_value = {'url': 'https://github.com/a/b/pull/1'}
            result = _detect_current_branch_pr()
        assert result == (None, None, None, None)

    def test_parses_various_url_formats(self):
        """Verify URL parsing for different GitHub URL patterns."""
        cases = [
            (
                'https://github.com/org/repo/pull/1',
                ('org', 'repo', '1', 'pr'),
            ),
            (
                'https://github.com/my-org/my-repo/pull/999',
                ('my-org', 'my-repo', '999', 'pr'),
            ),
        ]
        for url, expected in cases:
            data = {'number': int(expected[2]), 'url': url}
            with patch('thrds.platforms.github.commands.clone.proc') as mock_proc:
                mock_proc.json.return_value = data
                result = _detect_current_branch_pr()
            assert result == expected, f"Failed for URL: {url}"
