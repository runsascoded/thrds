"""Tests for `thrds.richtext` — rendering Slack's own parse tree to markdown.

Fixtures are shapes captured from live messages in the trainium and
cw-quickwins sessions (2026-08-19), which is where several of these cases came
from: the emoji element with no `name`, the bold span split across a code run,
and the nested list emitted as a sibling block. See `specs/rich-text-ingest.md`.
"""
from __future__ import annotations

import pytest

from thrds.richtext import RICH_TEXT_ENV, render, rich_text_enabled
from thrds.slack import SlackClient


def _rt(*elements: dict) -> list[dict]:
    return [{'type': 'rich_text', 'block_id': 'xY1', 'elements': list(elements)}]


def _sec(*elements: dict) -> dict:
    return {'type': 'rich_text_section', 'elements': list(elements)}


def _t(text: str, **style) -> dict:
    el = {'type': 'text', 'text': text}
    if style:
        el['style'] = style
    return el


# --- no rich_text → fall back ---


def test_render_none_without_a_rich_text_block():
    """None is the signal to use the regex path, so an unrecognized shape
    degrades instead of losing content."""
    assert render([{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'x'}}]) is None


def test_render_none_for_no_blocks():
    assert render(None) is None
    assert render([]) is None


# --- text runs and emphasis ---


def test_plain_text():
    assert render(_rt(_sec(_t('Hello.')))) == 'Hello.'


@pytest.mark.parametrize('style,expected', [
    ({'bold': True}, '**x**'),
    ({'italic': True}, '*x*'),
    ({'strike': True}, '~~x~~'),
    ({'code': True}, '`x`'),
])
def test_single_styles(style, expected):
    assert render(_rt(_sec(_t('x', **style)))) == expected


def test_bold_spanning_a_code_run():
    """Slack splits `**`x`, and y**` into a {code,bold} run and a {bold} run.
    Marking each run separately would emit ``` `x`**, and y** ``` — markers in
    the wrong place and a dangling pair."""
    assert render(_rt(_sec(
        _t('nki-library', code=True, bold=True),
        _t(', JAX PJRT plugin?', bold=True),
    ))) == '**`nki-library`, JAX PJRT plugin?**'


def test_emphasis_ends_where_the_style_does():
    assert render(_rt(_sec(
        _t('before '), _t('bold', bold=True), _t(' after'),
    ))) == 'before **bold** after'


def test_whitespace_is_hoisted_outside_the_markers():
    """`** bold **` is not emphasis in any markdown dialect."""
    assert render(_rt(_sec(_t(' bold ', bold=True)))) == ' **bold** '


def test_trailing_space_in_a_grouped_span_stays_outside():
    """The runs group, then the space is hoisted — `**a  **` would not be
    emphasis, and the space is between words either way."""
    assert render(_rt(_sec(_t('a', bold=True), _t('  ', bold=True)))) == '**a**  '


# --- links ---


def test_labelled_link():
    assert render(_rt(_sec({
        'type': 'link', 'url': 'https://ex.com/x', 'text': 'MWE gist',
    }))) == '[MWE gist](https://ex.com/x)'


def test_bare_link_without_a_label():
    assert render(_rt(_sec({'type': 'link', 'url': 'https://ex.com/x'}))) == 'https://ex.com/x'


def test_truncated_link_renders_bare():
    """A URL pasted into the composer comes back as `<url|shortened-display>`;
    the ellipsis is a rendering artifact, not something anyone wrote."""
    assert render(_rt(_sec({
        'type': 'link',
        'url': 'https://gist.github.com/ryan-williams/84242770b779b2be7f512d41f0d634a8',
        'text': 'gist.github.com/ryan-williams/…',
        'truncated': True,
    }))) == 'https://gist.github.com/ryan-williams/84242770b779b2be7f512d41f0d634a8'


def test_link_whose_label_is_its_url_renders_bare():
    assert render(_rt(_sec({
        'type': 'link', 'url': 'https://ex.com/x', 'text': 'https://ex.com/x',
    }))) == 'https://ex.com/x'


def test_link_with_a_code_label():
    assert render(_rt(_sec({
        'type': 'link', 'url': 'https://ex.com', 'text': 'nki-library#11',
        'style': {'code': True},
    }))) == '[`nki-library#11`](https://ex.com)'


# --- mentions and emoji ---


def test_user_mention():
    assert render(_rt(_sec({'type': 'user', 'user_id': 'W01729RUUG7'}))) == '<@W01729RUUG7>'


def test_channel_mention():
    assert render(_rt(_sec({'type': 'channel', 'channel_id': 'C0A'}))) == '<#C0A>'


def test_broadcast():
    assert render(_rt(_sec({'type': 'broadcast', 'range': 'here'}))) == '<!here>'


def test_custom_emoji_stays_a_shortcode():
    """It matches no standard alias, so it survives `decode_emoji` for
    `substitute_custom_emoji` to turn into an image link."""
    assert render(_rt(_sec({
        'type': 'emoji', 'name': 'claude', 'display_url': 'https://…/claude.png',
    }))) == ':claude:'


def test_standard_emoji_by_name():
    assert render(_rt(_sec({'type': 'emoji', 'name': 'grimacing'}))) == '😬'


def test_emoji_with_only_a_unicode_field():
    """Captured live: a unicode emoji typed directly carries no `name`, and
    reading only `name` renders it as an empty `::`."""
    assert render(_rt(_sec({'type': 'emoji', 'unicode': '26a0-fe0f'}))) == '⚠'


def test_emoji_with_neither_field():
    assert render(_rt(_sec({'type': 'emoji'}))) == ''


# --- code blocks ---


def test_preformatted_gets_its_own_fence_lines():
    """The bug `normalize_fences` exists for doesn't arise here: the content
    arrives with no fences, so there's nothing to disambiguate."""
    assert render(_rt({
        'type': 'rich_text_preformatted',
        'elements': [_t(' 7.53 TiB  a\n 0.00 TiB  b')],
    })) == '```\n 7.53 TiB  a\n 0.00 TiB  b\n```'


def test_preformatted_preserves_leading_whitespace():
    """Inside a code block that alignment is data, not indentation."""
    out = render(_rt({'type': 'rich_text_preformatted', 'elements': [_t('  x\ny')]}))
    assert out == '```\n  x\ny\n```'


def test_preformatted_with_a_trailing_newline_is_not_double_spaced():
    out = render(_rt({'type': 'rich_text_preformatted', 'elements': [_t('x\n')]}))
    assert out == '```\nx\n```'


def test_preformatted_after_a_section():
    assert render(_rt(
        _sec(_t('Listing:\n\n')),
        {'type': 'rich_text_preformatted', 'elements': [_t('a\nb')]},
        _sec(_t('\nAfter.')),
    )) == 'Listing:\n\n```\na\nb\n```\nAfter.'


# --- lists ---


def test_bullet_list_uses_the_local_marker():
    """Slack rewrites `- item` to `• item` in the stored `text`, so the regex
    path leaks bullet characters into docs."""
    assert render(_rt({
        'type': 'rich_text_list', 'style': 'bullet', 'indent': 0,
        'elements': [_sec(_t('one')), _sec(_t('two'))],
    })) == '- one\n- two'


def test_ordered_list_numbers_from_one():
    assert render(_rt({
        'type': 'rich_text_list', 'style': 'ordered', 'indent': 0,
        'elements': [_sec(_t('one')), _sec(_t('two'))],
    })) == '1. one\n2. two'


def test_nested_list_is_a_sibling_block_not_a_child():
    """Slack emits the sub-list beside its parent, so without an inserted
    newline every sub-item lands glued to the parent's last line."""
    assert render(_rt(
        {'type': 'rich_text_list', 'style': 'bullet', 'indent': 0,
         'elements': [_sec(_t('parent'))]},
        {'type': 'rich_text_list', 'style': 'bullet', 'indent': 1,
         'elements': [_sec(_t('child'))]},
    )) == '- parent\n    - child'


def test_list_after_a_section_that_ends_mid_line():
    assert render(_rt(
        _sec(_t('Intro:')),
        {'type': 'rich_text_list', 'style': 'bullet', 'indent': 0,
         'elements': [_sec(_t('one'))]},
    )) == 'Intro:\n- one'


def test_list_items_keep_their_inline_markup():
    assert render(_rt({
        'type': 'rich_text_list', 'style': 'bullet', 'indent': 0,
        'elements': [_sec(_t('use '), _t('x', code=True))],
    })) == '- use `x`'


# --- quotes ---


def test_quote():
    assert render(_rt({
        'type': 'rich_text_quote', 'elements': [_t('quoted\nlines')],
    })) == '> quoted\n> lines'


def test_quote_keeps_blank_lines_bare():
    assert render(_rt({
        'type': 'rich_text_quote', 'elements': [_t('a\n\nb')],
    })) == '> a\n>\n> b'


# --- unknown shapes degrade rather than drop content ---


def test_unknown_container_falls_back_to_concatenating_leaves():
    assert render(_rt({'type': 'rich_text_future', 'elements': [_t('kept')]})) == 'kept'


def test_unknown_leaf_keeps_its_text():
    assert render(_rt(_sec({'type': 'sticker', 'text': 'kept'}))) == 'kept'


# --- env toggle ---


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv(RICH_TEXT_ENV, raising=False)
    assert rich_text_enabled() is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'FALSE'])
def test_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv(RICH_TEXT_ENV, value)
    assert rich_text_enabled() is False


# --- _message_markdown: source selection ---


def test_message_markdown_prefers_rich_text():
    m = {'text': '*bold*', 'blocks': _rt(_sec(_t('from tree', bold=True)))}
    assert SlackClient._message_markdown(m) == '**from tree**'


def test_message_markdown_falls_back_to_text(monkeypatch):
    monkeypatch.setenv(RICH_TEXT_ENV, '0')
    m = {'text': '*bold*', 'blocks': _rt(_sec(_t('from tree', bold=True)))}
    assert SlackClient._message_markdown(m) == '**bold**'


def test_message_markdown_uses_the_section_of_a_finalized_message():
    """Chrome lives in a sibling context block there and never reaches here."""
    m = {'text': 'flattened', 'blocks': [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'a\n\nb'}},
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': '→ <#C0A>'}]},
    ]}
    assert SlackClient._message_markdown(m) == 'a\n\nb'


def test_message_markdown_drops_a_trailing_chrome_line():
    m = {
        'text': 'Body.\n\n→ <#C0A>',
        'blocks': _rt(_sec(_t('Body.\n\n')), _sec({'type': 'channel', 'channel_id': 'C0A'})),
    }
    assert SlackClient._message_markdown(m) == 'Body.'


def test_message_markdown_drops_a_leading_chrome_line():
    m = {
        'text': '→ <#C0A>\n\nBody.',
        'blocks': _rt(_sec({'type': 'channel', 'channel_id': 'C0A'}), _sec(_t('\n\nBody.'))),
    }
    assert SlackClient._message_markdown(m) == 'Body.'


def test_message_markdown_keeps_a_body_with_no_chrome():
    m = {'text': 'One.\n\nTwo.', 'blocks': _rt(_sec(_t('One.\n\nTwo.')))}
    assert SlackClient._message_markdown(m) == 'One.\n\nTwo.'


def test_entities_are_decoded_in_urls():
    """Slack HTML-encodes on storage and the tree is no exception: a permalink's
    `&cid=` arrives as `&amp;cid=` inside `url`, not just in the flat `text`."""
    assert render(_rt(_sec({
        'type': 'link',
        'url': 'https://ex.slack.com/archives/C0A/p1?thread_ts=1.1&amp;cid=C0A',
        'text': 'link',
    }))) == '[link](https://ex.slack.com/archives/C0A/p1?thread_ts=1.1&cid=C0A)'


def test_entities_are_decoded_in_text():
    assert render(_rt(_sec(_t('a &amp; b &lt;c&gt;')))) == 'a & b <c>'
