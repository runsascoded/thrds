"""Tests for PR/Issue spec parsing."""

import pytest

from thrds.platforms.github.patterns import parse_pr_spec, extract_title_from_first_line


class TestParseProSpec:
    """Test PR/Issue spec parsing in various formats."""

    def test_full_pr_url(self):
        """Test parsing full PR URL."""
        owner, repo, number, item_type = parse_pr_spec(
            'https://github.com/marin-community/marin/pull/1723'
        )
        assert owner == 'marin-community'
        assert repo == 'marin'
        assert number == '1723'
        assert item_type == 'pr'

    def test_full_issue_url(self):
        """Test parsing full Issue URL."""
        owner, repo, number, item_type = parse_pr_spec(
            'https://github.com/marin-community/marin/issues/1773'
        )
        assert owner == 'marin-community'
        assert repo == 'marin'
        assert number == '1773'
        assert item_type == 'issue'

    def test_owner_repo_number_format(self):
        """Test parsing owner/repo#number format."""
        owner, repo, number, item_type = parse_pr_spec('runsascoded/ghpr#42')
        assert owner == 'runsascoded'
        assert repo == 'ghpr'
        assert number == '42'
        assert item_type is None  # Type detection happens later

    def test_just_number(self):
        """Test parsing just a number (requires repo context)."""
        owner, repo, number, item_type = parse_pr_spec('1723')
        assert owner is None
        assert repo is None
        assert number == '1723'
        assert item_type is None

    def test_invalid_format(self):
        """Test parsing invalid format returns None values."""
        owner, repo, number, item_type = parse_pr_spec('invalid-format')
        assert owner is None
        assert repo is None
        assert number is None
        assert item_type is None

    def test_number_with_leading_zeros(self):
        """Test parsing number with leading zeros."""
        owner, repo, number, item_type = parse_pr_spec('0042')
        assert owner is None
        assert repo is None
        assert number == '0042'
        assert item_type is None


class TestExtractTitleFromFirstLine:
    """Test extracting title from first line of description."""

    def test_title_with_pr_reference(self):
        """Test extracting title when line has PR reference."""
        title = extract_title_from_first_line('# [owner/repo#123] This is the title')
        assert title == 'This is the title'

    def test_title_with_pr_link(self):
        """Test extracting title when line has PR link."""
        title = extract_title_from_first_line(
            '# [owner/repo#123](https://github.com/owner/repo/pull/123) Title here'
        )
        assert title == 'Title here'

    def test_simple_h1_title(self):
        """Test extracting simple H1 title."""
        title = extract_title_from_first_line('# Simple Title')
        assert title == 'Simple Title'

    def test_title_with_extra_whitespace(self):
        """Test extracting title with extra whitespace."""
        title = extract_title_from_first_line('  #   Whitespace   Title  ')
        assert title == 'Whitespace   Title'
