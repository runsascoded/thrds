"""Tests for `normalize_fences` — re-inflating Slack's compact code fences.

Slack lets a fenced block open and close flush against its content and renders
it correctly; CommonMark reads the first line as an info string and shows the
closing fence literally. A block composed in the Slack UI therefore arrives
readable in Slack and broken in the gist. See `specs/pull-fence-normalization.md`.
"""
from __future__ import annotations

import pytest

from thrds.mrkdwn import normalize_fences, to_markdown, to_slack

# The block from the spec, as Slack stored it after the draft was compressed
# in the UI, and as CommonMark needs it.
SLACK_FORM = (
    '``` 7.53 TiB  cudagraph-gate-a-pernode-cbon-20260813\n'
    ' 7.51 TiB  cudagraph-gate-c2-pergpu-cbon-20260814\n'
    ' 0.00 TiB  cudagraph-gate-cbase-pergpu-cboff-20260813```'
)
MD_FORM = (
    '```\n'
    ' 7.53 TiB  cudagraph-gate-a-pernode-cbon-20260813\n'
    ' 7.51 TiB  cudagraph-gate-c2-pergpu-cbon-20260814\n'
    ' 0.00 TiB  cudagraph-gate-cbase-pergpu-cboff-20260813\n'
    '```'
)


def test_the_reported_block():
    assert normalize_fences(SLACK_FORM) == MD_FORM


def test_leading_space_is_data_not_indentation():
    """Inside a code block the alignment is the content; trimming it would
    silently reformat a table."""
    assert normalize_fences('```  x\ny```') == '```\n  x\ny\n```'


def test_opening_fence_broken_from_its_content():
    assert normalize_fences('```first\nsecond\n```') == '```first\nsecond\n```'


def test_closing_fence_broken_off_its_content():
    assert normalize_fences('```\na\nb```') == '```\na\nb\n```'


def test_single_line_block_breaks_both_ends():
    assert normalize_fences('```a b```') == '```\na b\n```'


def test_a_bare_word_block_breaks_both_ends():
    """No newline in the body means there's no info string to preserve."""
    assert normalize_fences('```foo```') == '```\nfoo\n```'


def test_valid_commonmark_is_untouched():
    text = '```\nalready fine\n```'
    assert normalize_fences(text) == text


def test_info_string_is_preserved():
    """```python round-tripped from a local doc has to come back unchanged."""
    text = '```python\nx = 1\n```'
    assert normalize_fences(text) == text


def test_info_string_with_a_space_is_content():
    assert normalize_fences('```py 3\nx = 1\n```') == '```\npy 3\nx = 1\n```'


def test_prose_around_a_block_is_preserved():
    assert normalize_fences('Before.\n\n```a b```\n\nAfter.') == (
        'Before.\n\n```\na b\n```\n\nAfter.'
    )


def test_two_blocks_in_one_message():
    assert normalize_fences('```a b``` and ```c d```') == (
        '```\na b\n``` and ```\nc d\n```'
    )


def test_an_unpaired_fence_is_left_alone():
    """Guessing which side it is would be worse than leaving it."""
    assert normalize_fences('an unbalanced ``` fence') == 'an unbalanced ``` fence'


def test_no_fence_at_all():
    assert normalize_fences('plain text') == 'plain text'


def test_empty_block():
    assert normalize_fences('``````') == '``````'


def test_longer_fences_are_not_split():
    """CommonMark allows 4+ backticks; the run is one token."""
    assert normalize_fences('````a b````') == '````\na b\n````'


def test_inline_code_spans_are_untouched():
    """Non-goal: only fenced blocks."""
    text = 'use `foo` and `bar` here'
    assert normalize_fences(text) == text


# --- wired into to_markdown ---


def test_to_markdown_normalizes_fences():
    assert to_markdown(SLACK_FORM) == MD_FORM


def test_to_markdown_leaves_valid_fences_alone():
    text = '```\nalready fine\n```'
    assert to_markdown(text) == text


# --- round trip: `pull(push(x))` is stable for valid CommonMark ---


@pytest.mark.parametrize('doc', [
    '```\nplain block\n```',
    '```python\nx = 1\n```',
    'Before.\n\n```\n a\n b\n```\n\nAfter.',
    '```\nfirst\n```\n\ntext\n\n```\nsecond\n```',
    'no fences at all',
    '```\n 7.53 TiB  a\n 0.00 TiB  b\n```',
])
def test_round_trip_is_byte_stable(doc):
    """A file that was valid CommonMark stays byte-identical after a no-edit
    round trip, so drift detection doesn't fire on formatting alone."""
    assert to_markdown(to_slack(doc)) == doc


def test_round_trip_converges_after_one_pass():
    """The Slack-composed form normalizes once, then holds still."""
    once = to_markdown(SLACK_FORM)
    assert to_markdown(to_slack(once)) == once
