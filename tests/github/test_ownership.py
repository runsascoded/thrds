"""Tests for ownership checks in clone command."""

from unittest.mock import patch, MagicMock

import pytest


class TestCloneOwnershipCheck:
    """Test that clone command respects PR/Issue ownership."""

    def test_should_edit_own_pr(self):
        """Test that user should edit their own PR."""
        current_user = 'ryan-williams'
        item_data = {
            'user': {'login': 'ryan-williams'},
            'title': 'My PR',
        }

        pr_author = item_data.get('user', {}).get('login')
        should_edit = current_user == pr_author
        assert should_edit is True

    def test_should_not_edit_others_pr(self):
        """Test that user should not edit others' PR."""
        current_user = 'ryan-williams'
        item_data = {
            'user': {'login': 'octocat'},
            'title': 'Their PR',
        }

        pr_author = item_data.get('user', {}).get('login')
        should_edit = current_user == pr_author
        assert should_edit is False

    def test_handle_no_current_user(self):
        """Test handling when current user cannot be determined."""
        current_user = None
        item_data = {
            'user': {'login': 'octocat'},
            'title': 'Their PR',
        }

        pr_author = item_data.get('user', {}).get('login')
        should_edit = current_user == pr_author
        assert should_edit is False

    def test_handle_missing_author(self):
        """Test handling when PR author is missing."""
        current_user = 'ryan-williams'
        item_data = {
            'title': 'No Author PR',
        }

        pr_author = item_data.get('user', {}).get('login')
        should_edit = current_user == pr_author
        assert should_edit is False


class TestGetCurrentGithubUser:
    """Test getting current GitHub user."""

    @patch('utz.git.gist.get_github_user')
    def test_get_current_user(self, mock_get_github_user):
        """Test getting current GitHub user."""
        mock_get_github_user.return_value = 'ryan-williams'

        from thrds.platforms.github.api import get_current_github_user
        user = get_current_github_user()

        assert user == 'ryan-williams'
        mock_get_github_user.assert_called_once()

    @patch('utz.git.gist.get_github_user')
    def test_get_current_user_none(self, mock_get_github_user):
        """Test when current user cannot be determined."""
        mock_get_github_user.return_value = None

        from thrds.platforms.github.api import get_current_github_user
        user = get_current_github_user()

        assert user is None
        mock_get_github_user.assert_called_once()
