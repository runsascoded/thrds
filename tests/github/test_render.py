"""Tests for diff rendering utilities."""

import io
from unittest.mock import patch

import pytest

from thrds.platforms.github.render import render_unified_diff


class TestRenderUnifiedDiff:
    """Test unified diff rendering."""

    def test_no_newline_indicator_when_both_lack_newline(self):
        """Test that 'No newline' indicator doesn't appear when both sides lack it."""
        output = io.StringIO()

        def capture(msg):
            output.write(msg + '\n')

        render_unified_diff(
            remote_content='Line 1\nLine 2',  # No trailing newline
            local_content='Line 1\nLine 2 modified',  # No trailing newline
            fromfile='remote',
            tofile='local',
            use_color=False,
            log=capture,
        )

        result = output.getvalue()
        assert 'No newline at end of file' not in result

    def test_no_newline_indicator_when_both_have_newline(self):
        """Test that 'No newline' indicator doesn't appear when both have it."""
        output = io.StringIO()

        def capture(msg):
            output.write(msg + '\n')

        render_unified_diff(
            remote_content='Line 1\nLine 2\n',  # Has trailing newline
            local_content='Line 1\nLine 2 modified\n',  # Has trailing newline
            fromfile='remote',
            tofile='local',
            use_color=False,
            log=capture,
        )

        result = output.getvalue()
        assert 'No newline at end of file' not in result

    def test_no_newline_indicator_when_local_lacks_newline(self):
        """Test that 'No newline' indicator appears when local lacks trailing newline."""
        output = io.StringIO()

        def capture(msg):
            output.write(msg + '\n')

        # Content differs AND trailing newline differs
        render_unified_diff(
            remote_content='Line 1\nLine 2\n',  # Has trailing newline
            local_content='Line 1\nLine 2 modified',  # No trailing newline, different content
            fromfile='remote',
            tofile='local',
            use_color=False,
            log=capture,
        )

        result = output.getvalue()
        assert 'No newline at end of file' in result

    def test_no_newline_indicator_when_remote_lacks_newline(self):
        """Test that 'No newline' indicator appears when remote lacks trailing newline."""
        output = io.StringIO()

        def capture(msg):
            output.write(msg + '\n')

        # Content differs AND trailing newline differs
        render_unified_diff(
            remote_content='Line 1\nLine 2',  # No trailing newline
            local_content='Line 1\nLine 2 modified\n',  # Has trailing newline, different content
            fromfile='remote',
            tofile='local',
            use_color=False,
            log=capture,
        )

        result = output.getvalue()
        assert 'No newline at end of file' in result

    def test_only_trailing_newline_differs(self):
        """Test minimal diff when only trailing newline differs."""
        output = io.StringIO()

        def capture(msg):
            output.write(msg + '\n')

        render_unified_diff(
            remote_content='Same content\n',
            local_content='Same content',
            fromfile='remote',
            tofile='local',
            use_color=False,
            log=capture,
        )

        result = output.getvalue()
        assert 'Only trailing newline differs' in result
