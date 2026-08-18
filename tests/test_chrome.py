"""Tests for `thrds.chrome` — the staging-only footer line.

Chrome lives in the message *text* rather than in `blocks` because Slack makes
any message carrying blocks uneditable, and editing staged drafts (including
editing the footer itself, to retarget one) is what a staging channel is for.
"""
from __future__ import annotations

import pytest

from thrds.chrome import Chrome, gist_file_url, has_chrome, parse, render, split

PARENT = 'https://openathena.slack.com/archives/C0BN20081CH/p1786980761357209'
POSTED = 'https://openathena.slack.com/archives/C0P/p1786993740250899'


# --- gist_file_url ---


def test_gist_url_anchors_the_file():
    assert gist_file_url('abc123', '01-mfu.md') == (
        'https://gist.github.com/abc123#file-01-mfu-md'
    )


def test_gist_url_folds_runs_of_punctuation():
    assert gist_file_url('abc123', '02-tflops-q.md') == (
        'https://gist.github.com/abc123#file-02-tflops-q-md'
    )


def test_gist_url_lowercases():
    assert gist_file_url('abc123', '03-MFU.MD') == (
        'https://gist.github.com/abc123#file-03-mfu-md'
    )


# --- render ---


def _render(**kw) -> str | None:
    base = dict(channel=None, thread_ts=None, target_url=None,
                posted_url=None, gist_id=None, filename=None)
    return render(**{**base, **kw})


def test_render_top_level_target():
    assert _render(channel='C0T', gist_id='g1', filename='01-mfu.md') == (
        '→ <#C0T> · <https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>'
    )


def test_render_reply_target_links_the_arrow():
    """The ts is what a machine needs and a human never reads, so it's the
    arrow's href rather than visible text."""
    assert _render(channel='C0T', thread_ts='1786980761.357209', target_url=PARENT) == (
        f'<{PARENT}|→> (<#C0T>)'
    )


def test_render_reply_target_degrades_without_a_permalink():
    """Rather than printing a bare ts nobody reads."""
    assert _render(channel='C0T', thread_ts='1786980761.357209') == '→ <#C0T>'


def test_render_all_three_on_one_line():
    assert _render(
        channel='C0T', posted_url=POSTED, gist_id='g1', filename='01-mfu.md',
    ) == (
        f'→ <#C0T> · <{POSTED}|posted> · '
        f'<https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>'
    )


def test_render_gist_only_for_an_untargeted_draft():
    assert _render(gist_id='g1', filename='01-a.md') == (
        '<https://gist.github.com/g1#file-01-a-md|01-a.md>'
    )


def test_render_none_when_nothing_to_say():
    assert _render() is None


def test_render_omits_gist_without_a_filename():
    assert _render(channel='C0T', gist_id='g1') == '→ <#C0T>'


# --- parse ---


def test_parse_top_level():
    assert parse('→ <#C0T>') == Chrome(channel='C0T')


def test_parse_channel_mention_with_a_name():
    """Slack echoes mentions back as `<#C0T|name>` in some payloads."""
    assert parse('→ <#C0T|oa-amazon-trainium>') == Chrome(channel='C0T')


def test_parse_reply_recovers_the_ts_from_the_permalink():
    assert parse(f'<{PARENT}|→> (<#C0BN20081CH>)') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_pasted_permalink():
    """Authoring form: paste a message link after the arrow to aim a draft
    into that thread. Channel and ts both come from the URL."""
    assert parse(f'→ <{PARENT}>') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_pasted_permalink_without_angle_brackets():
    assert parse(f'→ {PARENT}') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_full_footer():
    line = (f'→ <#C0T> · <{POSTED}|posted> · '
            f'<https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>')
    assert parse(line) == Chrome(
        channel='C0T', filename='01-mfu.md', posted_url=POSTED,
    )


def test_parse_gist_only():
    assert parse('<https://gist.github.com/g1#file-01-a-md|01-a.md>') == Chrome(
        filename='01-a.md',
    )


@pytest.mark.parametrize('line', [
    'Just a sentence.',
    'A link: <https://example.com|docs>',
    '→ nowhere in particular',
    '→ <https://example.com/not-slack>',
    '· · ·',
    '',
])
def test_parse_rejects_non_footers(line):
    assert parse(line) is None


def test_parse_rejects_a_footer_with_an_unknown_segment():
    """Every segment must be a known shape — one stray part means the line is
    prose that happens to contain an arrow."""
    assert parse('→ <#C0T> · and then some') is None


def test_parse_rejects_a_target_segment_out_of_position():
    assert parse('<https://gist.github.com/g1#file-a-md|a.md> · → <#C0T>') is None


# --- split ---


def test_split_strips_the_footer():
    body = 'Para one.\n\nPara two.'
    text = f'{body}\n\n→ <#C0T>'
    assert split(text) == (body, Chrome(channel='C0T'))


def test_split_leaves_a_body_with_no_footer_untouched():
    text = 'Para one.\n\nPara two.'
    assert split(text) == (text, None)


def test_split_leaves_a_single_line_body_untouched():
    assert split('→ <#C0T>') == ('→ <#C0T>', None)


def test_split_preserves_interior_blank_lines():
    body = 'One.\n\n\nTwo.'
    assert split(f'{body}\n\n→ <#C0T>')[0] == body


def test_split_ignores_a_footer_shaped_line_mid_body():
    """Only the last line is chrome; anything above it is content."""
    text = '→ <#C0T>\n\nActual body.'
    assert split(text) == (text, None)


def test_has_chrome_is_the_promote_guard():
    assert has_chrome('Body.\n\n→ <#C0T>') is True
    assert has_chrome('Body.') is False
