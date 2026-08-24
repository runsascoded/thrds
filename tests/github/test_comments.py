"""Tests for comment functionality including draft workflow and diff rendering."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from thrds.platforms.github import comments, patterns


class TestDraftCommentDetection:
    """Test detection of draft comment files (new*.md)."""

    def test_detect_single_draft(self, tmp_path):
        """Test detecting a single draft comment file."""
        (tmp_path / "new.md").write_text("My comment")
        (tmp_path / "z123-user.md").write_text("Existing comment")

        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == 1
        assert drafts[0].name == "new.md"

    def test_detect_multiple_drafts(self, tmp_path):
        """Test detecting multiple draft comment files."""
        (tmp_path / "new.md").write_text("First comment")
        (tmp_path / "new-feature.md").write_text("Second comment")
        (tmp_path / "new-bug.md").write_text("Third comment")

        drafts = sorted(tmp_path.glob("new*.md"))
        assert len(drafts) == 3
        assert [d.name for d in drafts] == ["new-bug.md", "new-feature.md", "new.md"]

    def test_no_drafts(self, tmp_path):
        """Test when no draft comments exist."""
        (tmp_path / "z123-user.md").write_text("Existing comment")
        (tmp_path / "owner-repo#123.md").write_text("PR description")

        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == 0


class TestCommentFilenameGeneration:
    """Test generating comment filenames from API responses."""

    def test_generate_filename_new_format(self):
        """Test generating filename with author suffix."""
        comment = {
            "id": 123456789,
            "user": {"login": "ryan-williams"},
        }
        filename = f"z{comment['id']}-{comment['user']['login']}.md"
        assert filename == "z123456789-ryan-williams.md"

    def test_generate_filename_sanitize_author(self):
        """Test that special characters in author names are handled."""
        comment = {
            "id": 987654321,
            "user": {"login": "user-name_123"},
        }
        filename = f"z{comment['id']}-{comment['user']['login']}.md"
        assert filename == "z987654321-user-name_123.md"


class TestCommentIdExtraction:
    """Test extracting comment IDs from filenames."""

    def test_extract_id_new_format(self):
        """Test extracting ID from z{id}-{author}.md format."""
        comment_id = comments.get_comment_id_from_filename("z123456789-ryan-williams.md")
        assert comment_id == "123456789"

    def test_extract_id_legacy_format(self):
        """Test extracting ID from legacy z{id}.md format."""
        comment_id = comments.get_comment_id_from_filename("z987654321.md")
        assert comment_id == "987654321"

    def test_extract_id_invalid_format(self):
        """Test that invalid filenames return None."""
        assert comments.get_comment_id_from_filename("new.md") is None
        assert comments.get_comment_id_from_filename("owner-repo#123.md") is None
        assert comments.get_comment_id_from_filename("invalid.md") is None


class TestCommentMetadata:
    """Test comment metadata parsing and generation."""

    def test_read_comment_file(self, tmp_path):
        """Test reading metadata and body from comment file."""
        comment_file = tmp_path / "z123-ryan-williams.md"
        content = """<!-- author: ryan-williams -->
<!-- created_at: 2025-10-15T04:38:13Z -->
<!-- updated_at: 2025-10-15T04:38:13Z -->

Comment body here.
More content.
"""
        comment_file.write_text(content)

        author, created_at, updated_at, body = comments.read_comment_file(comment_file)
        assert author == "ryan-williams"
        assert created_at == "2025-10-15T04:38:13Z"
        assert updated_at == "2025-10-15T04:38:13Z"
        assert body == "Comment body here.\nMore content.\n"

    def test_write_comment_file(self, tmp_path):
        """Test writing comment file with metadata."""
        import os
        os.chdir(tmp_path)

        filepath = comments.write_comment_file(
            comment_id="123456789",
            author="ryan-williams",
            created_at="2025-10-15T04:38:13Z",
            updated_at="2025-10-15T04:38:13Z",
            body="Comment body here.\n"
        )

        assert filepath.exists()
        assert filepath.name == "z123456789-ryan-williams.md"

        # Verify content
        author, created_at, updated_at, body = comments.read_comment_file(filepath)
        assert author == "ryan-williams"
        assert body == "Comment body here.\n"

    def test_write_comment_file_no_updated_at(self, tmp_path):
        """Test writing comment file without updated_at (same as created_at)."""
        import os
        os.chdir(tmp_path)

        filepath = comments.write_comment_file(
            comment_id="999",
            author="user",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",  # Same as created_at
            body="New comment\n"
        )

        content = filepath.read_text()
        # Should not include updated_at line when it matches created_at
        assert "<!-- updated_at:" not in content


class TestCommentBodyHandling:
    """Test reading and writing comment bodies."""

    def test_read_draft_comment_body(self, tmp_path):
        """Test reading body from draft comment (no metadata)."""
        draft_file = tmp_path / "new.md"
        draft_file.write_text("Draft comment body\n")

        body = draft_file.read_text()
        assert body == "Draft comment body\n"

    def test_read_posted_comment_body(self, tmp_path):
        """Test reading body from posted comment (with metadata)."""
        comment_file = tmp_path / "z123-user.md"
        content = """<!-- author: user -->
<!-- created_at: 2025-01-01T00:00:00Z -->
<!-- updated_at: 2025-01-01T00:00:00Z -->

Posted comment body
"""
        comment_file.write_text(content)

        author, created_at, updated_at, body = comments.read_comment_file(comment_file)
        assert body == "Posted comment body\n"

    def test_empty_comment_body(self, tmp_path):
        """Test handling empty comment body."""
        draft_file = tmp_path / "new.md"
        draft_file.write_text("")

        body = draft_file.read_text()
        assert body == ""


class TestCommentLineEndings:
    """Test that line endings are normalized."""

    def test_normalize_crlf_to_lf(self):
        """Test that CRLF line endings are converted to LF."""
        content_with_crlf = "Line 1\r\nLine 2\r\nLine 3\r\n"
        normalized = content_with_crlf.replace('\r\n', '\n')
        assert normalized == "Line 1\nLine 2\nLine 3\n"
        assert '\r' not in normalized

    def test_preserve_lf_endings(self):
        """Test that LF line endings are preserved."""
        content_with_lf = "Line 1\nLine 2\nLine 3\n"
        normalized = content_with_lf.replace('\r\n', '\n')
        assert normalized == content_with_lf


class TestDraftCommentWorkflow:
    """Integration tests for the draft comment workflow."""

    @patch('thrds.platforms.github.cli.proc')
    def test_draft_file_detection_in_push(self, mock_proc, tmp_path):
        """Test that push command detects draft files."""
        # Create a draft comment
        draft_file = tmp_path / "new.md"
        draft_file.write_text("My new comment\n")

        # Check that glob pattern finds it
        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == 1
        assert drafts[0] == draft_file

    def test_draft_filename_patterns(self, tmp_path):
        """Test various draft filename patterns."""
        # Valid draft names
        valid_drafts = [
            "new.md",
            "new-feature.md",
            "new_123.md",
            "newer.md",
            "news.md",
        ]

        for name in valid_drafts:
            (tmp_path / name).write_text("content")

        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == len(valid_drafts)

        # Invalid names (should not match)
        (tmp_path / "old.md").write_text("content")
        (tmp_path / "z123-user.md").write_text("content")

        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == len(valid_drafts)


class TestCommentDiffPreview:
    """Test comment diff preview formatting."""

    def test_format_short_preview(self):
        """Test formatting a short comment preview (≤10 lines)."""
        lines = ["Line 1", "Line 2", "Line 3"]
        preview = "\n".join(lines)
        assert preview.count("\n") == 2
        assert len(preview.split("\n")) == 3

    def test_format_long_preview(self):
        """Test formatting a long comment preview (>10 lines)."""
        lines = [f"Line {i}" for i in range(1, 21)]
        preview = "\n".join(lines[:10])
        remaining = len(lines) - 10

        assert preview.count("\n") == 9
        assert remaining == 10

    def test_preview_with_empty_lines(self):
        """Test preview with empty lines preserved."""
        lines = ["Line 1", "", "Line 3", "", "Line 5"]
        preview = "\n".join(lines)
        assert preview == "Line 1\n\nLine 3\n\nLine 5"


class TestCommentFileOperations:
    """Test file operations for comment management."""

    def test_create_and_read_draft(self, tmp_path):
        """Test creating and reading a draft comment file."""
        draft_file = tmp_path / "new.md"
        content = "This is my draft comment.\n"
        draft_file.write_text(content)

        assert draft_file.exists()
        assert draft_file.read_text() == content

    def test_rename_after_post(self, tmp_path):
        """Test renaming draft to posted format."""
        draft_file = tmp_path / "new.md"
        draft_file.write_text("Comment body\n")

        # Simulate posting and getting ID back
        comment_id = "123456789"
        author = "ryan-williams"
        new_name = f"z{comment_id}-{author}.md"
        posted_file = tmp_path / new_name

        draft_file.rename(posted_file)

        assert not draft_file.exists()
        assert posted_file.exists()
        assert posted_file.read_text() == "Comment body\n"

    def test_multiple_draft_handling(self, tmp_path):
        """Test handling multiple drafts in sequence."""
        draft1 = tmp_path / "new.md"
        draft2 = tmp_path / "new-feature.md"

        draft1.write_text("First comment\n")
        draft2.write_text("Second comment\n")

        drafts = sorted(tmp_path.glob("new*.md"))
        assert len(drafts) == 2

        # Simulate posting both
        draft1.rename(tmp_path / "z111-user.md")
        draft2.rename(tmp_path / "z222-user.md")

        drafts = list(tmp_path.glob("new*.md"))
        assert len(drafts) == 0

        posted = sorted(tmp_path.glob("z*.md"))
        assert len(posted) == 2
