"""Tests for `NN-slug.md` thread-file naming and discovery (`thrds.threadfile`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from thrds.threadfile import (
    ThreadFile,
    next_index,
    parse_thread_filename,
    thread_filename,
    thread_files,
)


# --- thread_filename ---


def test_thread_filename_pads_index_to_two_digits():
    assert thread_filename(1, 'cw-quickwins') == '01-cw-quickwins.md'


def test_thread_filename_two_digit_index_unpadded():
    assert thread_filename(42, 'foo') == '42-foo.md'


def test_thread_filename_zero_index():
    assert thread_filename(0, 'preamble') == '00-preamble.md'


def test_thread_filename_rejects_negative_index():
    with pytest.raises(ValueError) as e:
        thread_filename(-1, 'foo')
    assert str(e.value) == 'Thread index must be non-negative, got -1'


def test_thread_filename_rejects_index_too_wide():
    """A 3-digit index would sort before 2-digit ones — reject rather than emit it."""
    with pytest.raises(ValueError) as e:
        thread_filename(100, 'foo')
    assert str(e.value) == (
        'Thread index 100 exceeds 2 digits; '
        'a session with >99 threads needs a wider prefix'
    )


def test_thread_filename_rejects_slug_with_invalid_chars():
    with pytest.raises(ValueError) as e:
        thread_filename(1, 'has spaces')
    assert str(e.value) == "Invalid thread slug 'has spaces': expected [a-zA-Z0-9_-]+"


# --- parse_thread_filename ---


def test_parse_thread_filename_round_trips():
    assert parse_thread_filename('01-cw-quickwins.md') == (1, 'cw-quickwins')


def test_parse_thread_filename_underscores_and_digits():
    assert parse_thread_filename('07-run_2_final.md') == (7, 'run_2_final')


@pytest.mark.parametrize('name', [
    'thrds.json',
    'README.md',
    'draft.md',
    '1-foo.md',            # single-digit prefix — not canonical
    '001-foo.md',          # three-digit prefix
    '01-foo.txt',
    '01-.md',              # empty slug
    'emoji-coreweave.png',
])
def test_parse_thread_filename_rejects_non_thread_files(name):
    assert parse_thread_filename(name) is None


# --- thread_files discovery ---


def _write(d: Path, name: str, text: str = 'body\n') -> None:
    (d / name).write_text(text)


def test_thread_files_sorted_by_index(tmp_path):
    for name in ['03-c.md', '01-a.md', '02-b.md']:
        _write(tmp_path, name)
    assert [(f.index, f.slug) for f in thread_files(tmp_path)] == [
        (1, 'a'), (2, 'b'), (3, 'c'),
    ]


def test_thread_files_ignores_non_thread_files(tmp_path):
    _write(tmp_path, '01-a.md')
    _write(tmp_path, 'thrds.json', '{}\n')
    _write(tmp_path, 'README.md')
    _write(tmp_path, 'emoji-x.png')
    assert [f.name for f in thread_files(tmp_path)] == ['01-a.md']


def test_thread_files_ignores_subdirectories(tmp_path):
    (tmp_path / '02-adir.md').mkdir()
    _write(tmp_path, '01-a.md')
    assert [f.name for f in thread_files(tmp_path)] == ['01-a.md']


def test_thread_files_empty_dir(tmp_path):
    assert thread_files(tmp_path) == []


def test_thread_files_missing_dir_returns_empty(tmp_path):
    assert thread_files(tmp_path / 'nope') == []


def test_thread_files_duplicate_slug_raises(tmp_path):
    _write(tmp_path, '01-foo.md')
    _write(tmp_path, '02-foo.md')
    with pytest.raises(ValueError) as e:
        thread_files(tmp_path)
    assert str(e.value) == "Duplicate thread slug 'foo': 01-foo.md and 02-foo.md"


def test_thread_files_returns_paths_under_session_dir(tmp_path):
    _write(tmp_path, '01-a.md')
    assert [f.path for f in thread_files(tmp_path)] == [tmp_path / '01-a.md']


def test_thread_file_name_property_is_canonical(tmp_path):
    _write(tmp_path, '05-x.md')
    assert [f.name for f in thread_files(tmp_path)] == ['05-x.md']


def test_thread_file_ordering_is_by_index_then_slug():
    a = ThreadFile(index=1, slug='b', path=Path('01-b.md'))
    b = ThreadFile(index=2, slug='a', path=Path('02-a.md'))
    assert sorted([b, a]) == [a, b]


# --- next_index ---


def test_next_index_of_empty_is_one():
    assert next_index([]) == 1


def test_next_index_is_one_past_highest(tmp_path):
    for name in ['01-a.md', '04-b.md', '02-c.md']:
        _write(tmp_path, name)
    assert next_index(thread_files(tmp_path)) == 5


def test_next_index_preserves_gaps(tmp_path):
    """Gaps are never compacted — renumbering renames files, and a rename
    breaks the per-file git history the layout exists to produce."""
    _write(tmp_path, '01-a.md')
    _write(tmp_path, '09-b.md')
    assert next_index(thread_files(tmp_path)) == 10
