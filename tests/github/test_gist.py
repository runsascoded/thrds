"""Tests for gist footer operations."""

import pytest

from thrds.platforms.github.gist import extract_gist_footer, add_gist_footer


class TestExtractGistFooter:
    """Test extracting gist footer from body text."""

    def test_extract_visible_footer(self):
        """Test extracting visible footer format."""
        # Visible footer format expects last 3 lines:
        # [-3]: empty line
        # [-2]: ---
        # [-1]: Synced with [gist](...)
        body = (
            'This is the body.\n'
            '\n'
            '---\n'
            'Synced with [gist](https://gist.github.com/abc123def456) via [ghpr](https://github.com/runsascoded/ghpr)'
        )

        body_without, gist_url = extract_gist_footer(body)

        assert body_without == 'This is the body.'
        assert gist_url == 'https://gist.github.com/abc123def456'

    def test_extract_hidden_footer_new_format(self):
        """Test extracting hidden footer with attribution."""
        body = (
            'This is the body.\n'
            '\n'
            '<!-- Synced with https://gist.github.com/abc123def456 via [ghpr](https://github.com/runsascoded/ghpr) -->'
        )

        body_without, gist_url = extract_gist_footer(body)

        assert body_without == 'This is the body.'
        assert gist_url == 'https://gist.github.com/abc123def456'

    def test_extract_hidden_footer_with_revision(self):
        """Test extracting hidden footer with revision."""
        body = (
            'This is the body.\n'
            '\n'
            '<!-- Synced with https://gist.github.com/abc123def456/0123456789abcdef via [ghpr](https://github.com/runsascoded/ghpr) -->'
        )

        body_without, gist_url = extract_gist_footer(body)

        assert body_without == 'This is the body.'
        assert gist_url == 'https://gist.github.com/abc123def456/0123456789abcdef'

    def test_no_footer(self):
        """Test when there is no footer."""
        body = 'This is just a body with no footer.\n'

        body_without, gist_url = extract_gist_footer(body)

        assert body_without == body
        assert gist_url is None

    def test_empty_body(self):
        """Test with empty body."""
        body_without, gist_url = extract_gist_footer('')

        assert body_without == ''
        assert gist_url is None

    def test_none_body(self):
        """Test with None body."""
        body_without, gist_url = extract_gist_footer(None)

        assert body_without is None
        assert gist_url is None

    def test_footer_like_content_not_at_end(self):
        """Test that footer-like content in the middle is ignored."""
        body = (
            'This mentions https://gist.github.com/abc123def456 in the text.\n'
            '\n'
            'More content here.'
        )

        body_without, gist_url = extract_gist_footer(body)

        assert body_without == body
        assert gist_url is None

    def test_multiline_body_with_footer(self):
        """Test multiline body with footer."""
        body = (
            'Line 1\n'
            '\n'
            'Line 2 with **markdown**\n'
            '\n'
            '- List item\n'
            '\n'
            '<!-- Synced with https://gist.github.com/fedcba987654 via [ghpr](https://github.com/runsascoded/ghpr) -->'
        )

        body_without, gist_url = extract_gist_footer(body)

        expected_body = (
            'Line 1\n'
            '\n'
            'Line 2 with **markdown**\n'
            '\n'
            '- List item'
        )
        assert body_without == expected_body
        assert gist_url == 'https://gist.github.com/fedcba987654'


class TestAddGistFooter:
    """Test adding gist footer to body text."""

    def test_add_hidden_footer(self):
        """Test adding hidden footer."""
        body = 'This is the body.'
        gist_url = 'https://gist.github.com/abc123def456'

        result = add_gist_footer(body, gist_url, visible=False)

        expected = 'This is the body.\n\n<!-- Synced with https://gist.github.com/abc123def456 via [ghpr](https://github.com/runsascoded/ghpr) -->'
        assert result == expected

    def test_add_visible_footer(self):
        """Test adding visible footer."""
        body = 'This is the body.'
        gist_url = 'https://gist.github.com/abc123def456'

        result = add_gist_footer(body, gist_url, visible=True)

        expected = 'This is the body.\n\n\n---\n\nSynced with [gist](https://gist.github.com/abc123def456) via [ghpr](https://github.com/runsascoded/ghpr)'
        assert result == expected

    def test_add_footer_to_empty_body(self):
        """Test adding footer to empty body."""
        gist_url = 'https://gist.github.com/abc123def456'

        result = add_gist_footer('', gist_url, visible=False)

        assert result == '<!-- Synced with https://gist.github.com/abc123def456 via [ghpr](https://github.com/runsascoded/ghpr) -->'

    def test_add_footer_to_none_body(self):
        """Test adding footer to None body."""
        gist_url = 'https://gist.github.com/abc123def456'

        result = add_gist_footer(None, gist_url, visible=False)

        assert result == '<!-- Synced with https://gist.github.com/abc123def456 via [ghpr](https://github.com/runsascoded/ghpr) -->'

    def test_replace_existing_footer(self):
        """Test that existing footer is replaced, not duplicated."""
        body = (
            'This is the body.\n'
            '\n'
            '<!-- Synced with https://gist.github.com/0123456789ab via [ghpr](https://github.com/runsascoded/ghpr) -->'
        )
        new_gist_url = 'https://gist.github.com/fedcba987654'

        result = add_gist_footer(body, new_gist_url, visible=False)

        expected = 'This is the body.\n\n<!-- Synced with https://gist.github.com/fedcba987654 via [ghpr](https://github.com/runsascoded/ghpr) -->'
        assert result == expected

    def test_visible_footer_with_revision(self):
        """Test visible footer preserves revision in URL."""
        body = 'This is the body.'
        gist_url = 'https://gist.github.com/username/abc123def456/0fedcba987654321'

        result = add_gist_footer(body, gist_url, visible=True)

        expected = 'This is the body.\n\n\n---\n\nSynced with [gist](https://gist.github.com/abc123def456/0fedcba987654321) via [ghpr](https://github.com/runsascoded/ghpr)'
        assert result == expected

    def test_add_footer_preserves_body_formatting(self):
        """Test that adding footer preserves body formatting."""
        body = (
            'Line 1\n'
            '\n'
            'Line 2 with **markdown**\n'
            '\n'
            '- List item'
        )
        gist_url = 'https://gist.github.com/abc123def456'

        result = add_gist_footer(body, gist_url, visible=False)

        expected = (
            'Line 1\n'
            '\n'
            'Line 2 with **markdown**\n'
            '\n'
            '- List item\n'
            '\n'
            '<!-- Synced with https://gist.github.com/abc123def456 via [ghpr](https://github.com/runsascoded/ghpr) -->'
        )
        assert result == expected


class TestExtractThenAddRoundtrip:
    """Test extracting and adding footer preserves body."""

    def test_roundtrip_hidden_footer(self):
        """Test extracting then re-adding hidden footer."""
        original_body = 'This is the original body.'
        gist_url = 'https://gist.github.com/abc123def456'

        # Add footer
        with_footer = add_gist_footer(original_body, gist_url, visible=False)

        # Extract footer
        body_without, extracted_url = extract_gist_footer(with_footer)

        assert body_without == original_body
        assert extracted_url == gist_url

    def test_roundtrip_visible_footer(self):
        """Test extracting then re-adding visible footer."""
        original_body = 'This is the original body.'
        gist_url = 'https://gist.github.com/abc123def456'

        # Add footer
        with_footer = add_gist_footer(original_body, gist_url, visible=True)

        # Extract footer
        body_without, extracted_url = extract_gist_footer(with_footer)

        assert body_without == original_body
        assert extracted_url == gist_url

    def test_roundtrip_multiline_body(self):
        """Test roundtrip with multiline body."""
        original_body = (
            'Line 1\n'
            '\n'
            'Line 2 with **markdown**\n'
            '\n'
            '- List item'
        )
        gist_url = 'https://gist.github.com/fedcba987654'

        # Add footer
        with_footer = add_gist_footer(original_body, gist_url, visible=False)

        # Extract footer
        body_without, extracted_url = extract_gist_footer(with_footer)

        assert body_without == original_body
        assert extracted_url == gist_url
