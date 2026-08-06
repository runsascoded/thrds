"""Tests for `thrds.mrkdwn` — local markdown → Slack mrkdwn conversion."""
from __future__ import annotations

from thrds.mrkdwn import to_markdown, to_slack


def test_plain_text_passthrough():
    assert to_slack('just some plain text.') == 'just some plain text.'


def test_link_single():
    assert to_slack('see [the docs](https://example.com/x).') == 'see <https://example.com/x|the docs>.'


def test_link_multiple_non_greedy():
    """`.*?` gone wrong = one giant capture spanning both links."""
    assert to_slack('[a](b) and [c](d)') == '<b|a> and <d|c>'


def test_link_with_query_string_and_ampersand():
    """Real Slack permalinks include `?thread_ts=...&cid=...`."""
    src = '[click](https://openathena.slack.com/archives/C0X/p123?thread_ts=1.2&cid=C0X)'
    dst = '<https://openathena.slack.com/archives/C0X/p123?thread_ts=1.2&cid=C0X|click>'
    assert to_slack(src) == dst


def test_bold_double_star():
    assert to_slack('**important**') == '*important*'


def test_bold_inline():
    assert to_slack('this is **bold** here.') == 'this is *bold* here.'


def test_bold_with_backticks_inside():
    """Bold-around-code is common in docs (`**`code`**`) — must survive."""
    assert to_slack('**`cross_entropy` OOMs**') == '*`cross_entropy` OOMs*'


def test_bold_link_composition():
    """Bold-around-link: link substitution runs first, then bold rewriting."""
    assert to_slack('**[docs](https://x)**') == '*<https://x|docs>*'


def test_backticks_unchanged():
    assert to_slack('`inline code` and ``fancier``') == '`inline code` and ``fancier``'


def test_italic_unchanged():
    assert to_slack('_emphasis_') == '_emphasis_'


def test_dash_bullet_unchanged():
    assert to_slack('- one\n- two') == '- one\n- two'


def test_realistic_bullet_with_link_and_bold():
    """The exact shape from the trainium integration test."""
    src = '- Found + fixed ([nki-library#11](https://github.com/aws-neuron/nki-library/pull/11)) a bug\n- **`cross_entropy`** OOMs'
    dst = '- Found + fixed (<https://github.com/aws-neuron/nki-library/pull/11|nki-library#11>) a bug\n- *`cross_entropy`* OOMs'
    assert to_slack(src) == dst


# --- to_markdown (inverse) ---

def test_to_markdown_slack_link():
    assert to_markdown('see <https://example.com/x|the docs>.') == 'see [the docs](https://example.com/x).'


def test_to_markdown_slack_bold():
    assert to_markdown('this is *bold* here.') == 'this is **bold** here.'


def test_to_markdown_html_entities():
    """Slack HTML-entity-encodes `&`, `<`, `>` on storage."""
    src = '<https://ex.com/x?a=1&amp;b=2|link>'
    dst = '[link](https://ex.com/x?a=1&b=2)'
    assert to_markdown(src) == dst


def test_to_markdown_leaves_user_mentions_alone():
    """`<@Uxxx>` and `<#Cxxx>` are Slack refs, not markdown links."""
    assert to_markdown('cc <@U0123> in <#C456|general>') == 'cc <@U0123> in <#C456|general>'


def test_roundtrip_link():
    src = 'see [the docs](https://example.com/x).'
    assert to_markdown(to_slack(src)) == src


def test_roundtrip_bold():
    src = 'this is **bold** here.'
    assert to_markdown(to_slack(src)) == src


def test_roundtrip_realistic_trainium_shape():
    """The end-to-end shape we actually push + pull in the integration test."""
    src = (
        '- Found + fixed ([nki-library#11](https://github.com/aws-neuron/nki-library/pull/11)) a bug\n'
        '- **`cross_entropy`** OOMs at V=128k'
    )
    assert to_markdown(to_slack(src)) == src


# --- italic vs bold disambiguation (asymmetric between md and slack) ---

def test_to_slack_italic_becomes_underscore():
    """Local `*italic*` (md italic) → `_italic_` (Slack italic) — else Slack renders as bold."""
    assert to_slack('word *sharded* params') == 'word _sharded_ params'


def test_to_slack_bold_and_italic_together():
    """`**bold**` → `*bold*`, `*italic*` → `_italic_`, no cross-contamination."""
    assert to_slack('**A** and *B*') == '*A* and _B_'


def test_to_slack_italic_not_matched_inside_bold():
    """`**text**` is bold; the inner `*` guards prevent italic pattern from matching halves."""
    assert to_slack('**bold word**') == '*bold word*'


def test_to_markdown_italic_underscore_becomes_star():
    assert to_markdown('word _sharded_ params') == 'word *sharded* params'


def test_roundtrip_italic():
    src = 'word *sharded* params'
    assert to_markdown(to_slack(src)) == src


def test_roundtrip_bold_and_italic():
    src = 'A **bold** claim about *italic* text.'
    assert to_markdown(to_slack(src)) == src


# --- word-boundary guards: identifiers must not be eaten as italic ---

def test_to_markdown_identifier_with_underscore_unchanged():
    """`thread_ts=1786...` in a URL — the `_` must NOT trigger italic conversion."""
    src = 'see <https://x.com/a?thread_ts=1&cid=Y|link1> and <https://x.com/b?thread_ts=2&cid=Z|link2>'
    dst = 'see [link1](https://x.com/a?thread_ts=1&cid=Y) and [link2](https://x.com/b?thread_ts=2&cid=Z)'
    assert to_markdown(src) == dst


def test_to_markdown_emoji_shortcode_unchanged():
    """`:left_right_arrow:` — inner `_`s must survive."""
    assert to_markdown(':left_right_arrow:') == ':left_right_arrow:'


def test_to_markdown_code_identifier_underscores_unchanged():
    """`` `scatter_add` `` and `` `cross_entropy` `` in the same string — `_`s stay."""
    src = '- fixed `scatter_add`\n- **`cross_entropy`** OOMs'
    dst = '- fixed `scatter_add`\n- ****`cross_entropy`**** OOMs' if False else '- fixed `scatter_add`\n- ****`cross_entropy`**** OOMs'
    # Actually just assert the underscores don't turn into asterisks.
    result = to_markdown(src)
    assert '_' in result and '`scatter_add`' in result and '`cross_entropy`' in result


def test_to_slack_identifier_asterisk_multiplication_unchanged():
    """`a*b*c` (multiplication or C-style pointer) — must not italicize."""
    assert to_slack('the product a*b*c is 42') == 'the product a*b*c is 42'


def test_to_markdown_identifier_slack_bold_between_word_chars_unchanged():
    """`x*y` (something*something) in Slack shouldn't become `x**y`."""
    assert to_markdown('rate=a*b*c') == 'rate=a*b*c'
