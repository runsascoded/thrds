"""Tests for description file operations."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from thrds.platforms.github.files import (
    get_expected_description_filename,
    find_description_file,
    write_description_with_link_ref,
    read_description_file,
)


class TestGetExpectedDescriptionFilename:
    """Test generating expected description filename."""

    def test_with_repo_and_number(self):
        """Test generating filename with repo and PR number."""
        filename = get_expected_description_filename('owner', 'myrepo', '123')
        assert filename == 'myrepo#123.md'

    def test_with_repo_and_number_int(self):
        """Test generating filename with integer PR number."""
        filename = get_expected_description_filename('owner', 'myrepo', 123)
        assert filename == 'myrepo#123.md'

    def test_without_repo(self):
        """Test fallback to DESCRIPTION.md without repo."""
        filename = get_expected_description_filename(owner='owner', pr_number='123')
        assert filename == 'DESCRIPTION.md'

    def test_without_number(self):
        """Test fallback to DESCRIPTION.md without PR number."""
        filename = get_expected_description_filename(owner='owner', repo='myrepo')
        assert filename == 'DESCRIPTION.md'

    def test_no_params(self):
        """Test fallback to DESCRIPTION.md with no params."""
        filename = get_expected_description_filename()
        assert filename == 'DESCRIPTION.md'


class TestFindDescriptionFile:
    """Test finding description files."""

    def test_find_pr_specific_file(self):
        """Test finding PR-specific description file."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            desc_file = tmppath / 'myrepo#123.md'
            desc_file.write_text('# Test PR')

            found = find_description_file(tmppath)
            assert found is not None
            assert found.name == 'myrepo#123.md'

    def test_find_description_md_fallback(self):
        """Test finding DESCRIPTION.md as fallback."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            desc_file = tmppath / 'DESCRIPTION.md'
            desc_file.write_text('# Test Description')

            found = find_description_file(tmppath)
            assert found is not None
            assert found.name == 'DESCRIPTION.md'

    def test_pr_specific_takes_precedence(self):
        """Test that PR-specific file takes precedence over DESCRIPTION.md."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / 'DESCRIPTION.md').write_text('# Generic')
            (tmppath / 'myrepo#123.md').write_text('# Specific')

            found = find_description_file(tmppath)
            assert found is not None
            assert found.name == 'myrepo#123.md'

    def test_no_description_file(self):
        """Test when no description file exists."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            found = find_description_file(tmppath)
            assert found is None

    def test_ignore_non_pr_hash_files(self):
        """Test that files with # but wrong format are ignored."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / 'not-a-pr#file.txt').write_text('Not a PR')
            (tmppath / '#random.md').write_text('Random')

            found = find_description_file(tmppath)
            assert found is None


class TestWriteDescriptionWithLinkRef:
    """Test writing description files with link references."""

    def test_write_basic_description(self):
        """Test writing a basic description."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#123.md'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='123',
                title='Test PR',
                body='This is the body.',
                url='https://github.com/owner/myrepo/pull/123'
            )

            content = filepath.read_text()
            expected = (
                '# [owner/myrepo#123] Test PR\n'
                '\n'
                'This is the body.\n'
                '\n'
                '[owner/myrepo#123]: https://github.com/owner/myrepo/pull/123\n'
            )
            assert content == expected

    def test_write_empty_body(self):
        """Test writing description with empty body."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#456.md'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='456',
                title='Empty Body',
                body='',
                url='https://github.com/owner/myrepo/pull/456'
            )

            content = filepath.read_text()
            expected = (
                '# [owner/myrepo#456] Empty Body\n'
                '\n'
                '[owner/myrepo#456]: https://github.com/owner/myrepo/pull/456\n'
            )
            assert content == expected

    def test_write_multiline_body(self):
        """Test writing description with multiline body."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#789.md'
            body = 'Line 1\n\nLine 2 with **markdown**\n\n- List item\n'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='789',
                title='Multi Line',
                body=body,
                url='https://github.com/owner/myrepo/pull/789'
            )

            content = filepath.read_text()
            # Body already has trailing newline, don't add another
            expected = (
                '# [owner/myrepo#789] Multi Line\n'
                '\n'
                f'{body}'
                '\n'
                '[owner/myrepo#789]: https://github.com/owner/myrepo/pull/789\n'
            )
            assert content == expected

    def test_body_with_existing_link_def(self):
        """Test that existing link definition in body is not duplicated."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#999.md'
            body = 'Some content.\n\n[owner/myrepo#999]: https://github.com/owner/myrepo/pull/999\n'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='999',
                title='Existing Link',
                body=body,
                url='https://github.com/owner/myrepo/pull/999'
            )

            content = filepath.read_text()
            # Count occurrences of the link definition
            link_def = '[owner/myrepo#999]: https://github.com/owner/myrepo/pull/999'
            assert content.count(link_def) == 1

    def test_body_with_existing_link_def_no_trailing_newline(self):
        """Test that file ends with newline even when body (with link) lacks one.

        GitHub strips trailing newlines from PR descriptions. When the body
        already contains the link definition but lacks a trailing newline,
        we should still ensure the file ends with one.
        """
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#888.md'
            # Body has link def but no trailing newline (simulates GitHub stripping it)
            body = 'Some content.\n\n[owner/myrepo#888]: https://github.com/owner/myrepo/pull/888'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='888',
                title='No Trailing Newline',
                body=body,
                url='https://github.com/owner/myrepo/pull/888'
            )

            content = filepath.read_text()
            # File should end with newline
            assert content.endswith('\n'), "File should end with a newline"
            # Link def should appear exactly once
            link_def = '[owner/myrepo#888]: https://github.com/owner/myrepo/pull/888'
            assert content.count(link_def) == 1


class TestReadDescriptionFile:
    """Test reading and parsing description files."""

    def test_read_link_ref_style(self):
        """Test reading link-reference style description."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#123.md'
            filepath.write_text(
                '# [owner/myrepo#123] Test Title\n'
                '\n'
                'Body content here.\n'
                '\n'
                '[owner/myrepo#123]: https://github.com/owner/myrepo/pull/123\n'
            )

            title, body = read_description_file(tmppath)

            assert title == 'Test Title'
            assert body == 'Body content here.'

    def test_read_inline_link_style(self):
        """Test reading inline link style description."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#456.md'
            filepath.write_text(
                '# [owner/myrepo#456](https://github.com/owner/myrepo/pull/456) Inline Link\n'
                '\n'
                'Inline body content.\n'
            )

            title, body = read_description_file(tmppath)

            assert title == 'Inline Link'
            assert body == 'Inline body content.'

    def test_read_simple_h1_fallback(self):
        """Test reading simple H1 title as fallback."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'DESCRIPTION.md'
            filepath.write_text(
                '# Simple Title\n'
                '\n'
                'Simple body.\n'
            )

            title, body = read_description_file(tmppath)

            assert title == 'Simple Title'
            assert body == 'Simple body.'

    def test_read_multiline_body(self):
        """Test reading multiline body."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#789.md'
            filepath.write_text(
                '# [owner/myrepo#789] Multi Line\n'
                '\n'
                'Line 1\n'
                '\n'
                'Line 2 with **markdown**\n'
                '\n'
                '- List item\n'
                '\n'
                '[owner/myrepo#789]: https://github.com/owner/myrepo/pull/789\n'
            )

            title, body = read_description_file(tmppath)

            assert title == 'Multi Line'
            # Note: Link definitions are stripped, trailing whitespace removed
            expected_body = 'Line 1\n\nLine 2 with **markdown**\n\n- List item'
            assert body == expected_body

    def test_read_no_description_file(self):
        """Test reading when no description file exists."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            title, body = read_description_file(tmppath)
            assert title is None
            assert body is None

    def test_write_then_read_roundtrip(self):
        """Test writing then reading preserves title and body."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            filepath = tmppath / 'myrepo#555.md'
            original_title = 'Roundtrip Test'
            original_body = 'Test body\n\nWith multiple lines'

            write_description_with_link_ref(
                filepath,
                owner='owner',
                repo='myrepo',
                pr_number='555',
                title=original_title,
                body=original_body,
                url='https://github.com/owner/myrepo/pull/555'
            )

            title, body = read_description_file(tmppath)

            assert title == original_title
            assert body == original_body
