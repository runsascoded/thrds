"""Render Slack's `rich_text` blocks — its own parse tree — into local markdown.

Slack attaches a ``rich_text`` block to every message it parses, and that block
is the structure `mrkdwn.to_markdown` otherwise has to reverse-engineer out of
the flat ``text`` field with regexes. Reading it instead removes a whole class
of ambiguity rather than patching instances of it:

- A code block arrives as ``rich_text_preformatted`` holding its content
  verbatim, with **no fences** — so there is nothing to disambiguate between a
  language tag and a first line that happens to be one word (the bug
  `mrkdwn.normalize_fences` exists to paper over).
- Inline code, bold and italic arrive as ``style`` flags on text runs, so
  emphasis inside a code span can't be misread, and the word-boundary guard
  that keeps ``thread_ts=1&cid=X thread_ts=2`` from becoming an italic span
  across half a URL isn't needed.
- Links, channel and user mentions arrive structured, so there is no
  ``<url|text>`` regex to sequence against anything else.

`mrkdwn.to_markdown` stays as the fallback for messages that carry no
``rich_text``: our own finalized posts (which carry ``section``/``context``
blocks instead) and anything Slack renders unusually. See
`specs/rich-text-ingest.md`.

**Round-trip is the constraint, not fidelity.** The output has to invert
`mrkdwn.to_slack` — reproducing the local dialect — rather than be canonical
CommonMark, or the first pull after this lands rewrites every file in every
session and every drift check fires.
"""
from __future__ import annotations

import os
from itertools import groupby

from .mrkdwn import decode_emoji, decode_entities

# Escape hatch. Set to `0`/`false`/`no` to fall back to parsing `text` with
# regexes — the path this replaced, kept reachable in case a message shape
# renders worse through the tree than through the flat text.
RICH_TEXT_ENV = 'THRDS_RICH_TEXT'


def rich_text_enabled() -> bool:
    return os.environ.get(RICH_TEXT_ENV, '1').lower() not in ('0', 'false', 'no')

# What a nested list item is indented by in the local dialect. Four spaces,
# matching what the sessions already contain.
_LIST_INDENT = '    '

# Blocks that start their own line. Sections carry their own newlines; these
# don't, so a separator has to be inserted before them.
_BLOCK_LEVEL = ('rich_text_list', 'rich_text_preformatted', 'rich_text_quote')


def _emphasis(el: dict) -> tuple[bool, bool, bool]:
    """The run's ``(bold, italic, strike)`` — everything but ``code``.

    Emphasis is the part that spans runs: Slack splits ``**`x`, and y**`` into
    a ``{code, bold}`` run and a ``{bold}`` run, so applying markers per-run
    would emit ``` `x`**, and y** ``` — markers in the wrong place and a
    dangling pair. Grouping consecutive runs by this key puts one pair around
    the whole span, which is what the author wrote.
    """
    style = el.get('style') or {}
    return bool(style.get('bold')), bool(style.get('italic')), bool(style.get('strike'))


def _wrap(text: str, emphasis: tuple[bool, bool, bool]) -> str:
    """Wrap ``text`` in markers for the emphasis it shares.

    Surrounding whitespace is hoisted outside the markers — ``** bold **`` is
    not emphasis in any markdown dialect, and Slack does put spaces inside
    styled runs.
    """
    bold, italic, strike = emphasis
    if not (bold or italic or strike) or not text.strip():
        return text
    core = text.strip()
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if strike:
        core = f'~~{core}~~'
    if bold:
        core = f'**{core}**'
    if italic:
        core = f'*{core}*'
    return f'{lead}{core}{trail}'


def _code(text: str, style: dict | None) -> str:
    """Code spans apply per-run, since a code span can't span other markup."""
    return f'`{text}`' if (style or {}).get('code') else text


def _link(el: dict) -> str:
    """A link element as markdown, preferring a bare URL where the label is noise.

    Slack stores a URL pasted into the composer as ``<url|shortened-display>``
    with ``truncated: true`` — the label is a rendering artifact
    (``gist.github.com/ryan-williams/…``), not something anyone wrote. Emitting
    it as a markdown label would bake an ellipsis into the doc; the bare URL is
    what was meant, and is what `to_slack` produces on the way back out.
    """
    url = el.get('url', '')
    text = el.get('text')
    if not text or el.get('truncated') or text == url:
        return url
    return f'[{_code(text, el.get("style"))}]({url})'


def _emoji(el: dict) -> str:
    """An emoji element as a shortcode, or its literal character.

    Slack sends one of two shapes and the ``name`` is not always there: a
    unicode emoji typed directly arrives as ``{"unicode": "26a0-fe0f"}`` with
    no name at all, and reading only ``name`` renders it as an empty ``::``.
    Custom workspace emoji always carry a name and no unicode; those stay
    shortcodes for `substitute_custom_emoji` to turn into image links.
    """
    name = el.get('name')
    if name:
        return f':{name}:'
    codepoints = el.get('unicode')
    if codepoints:
        return ''.join(chr(int(cp, 16)) for cp in codepoints.split('-'))
    return ''


def _leaf(el: dict) -> str:
    kind = el.get('type')
    if kind == 'text':
        return _code(el.get('text', ''), el.get('style'))
    if kind == 'link':
        return _link(el)
    if kind == 'emoji':
        return _emoji(el)
    if kind == 'user':
        return f'<@{el.get("user_id", "")}>'
    if kind == 'channel':
        return f'<#{el.get("channel_id", "")}>'
    if kind == 'usergroup':
        return f'<!subteam^{el.get("usergroup_id", "")}>'
    if kind == 'broadcast':
        return f'<!{el.get("range", "here")}>'
    # Unknown leaf: fall back to whatever text it carries rather than dropping
    # content on the floor for a type Slack adds later.
    return el.get('text', '')


def _section(el: dict) -> str:
    """Leaves, with consecutive same-emphasis runs wrapped as one span."""
    out = []
    for emphasis, group in groupby(el.get('elements', []), key=_emphasis):
        out.append(_wrap(''.join(_leaf(c) for c in group), emphasis))
    return ''.join(out)


def _preformatted(el: dict) -> str:
    """A fenced block, always with its fences on their own lines.

    The content is emitted byte-for-byte — inside a code block, leading
    whitespace is data (a right-aligned size column), not indentation.
    """
    body = ''.join(_leaf(c) for c in el.get('elements', []))
    if body.endswith('\n'):
        return f'```\n{body}```'
    return f'```\n{body}\n```'


def _quote(el: dict) -> str:
    body = ''.join(_leaf(c) for c in el.get('elements', []))
    return '\n'.join(f'> {line}' if line else '>' for line in body.split('\n'))


def _list(el: dict) -> str:
    """A list, rendered back to the ``- ``/``1. `` markers a local doc uses.

    Slack rewrites ``- item`` to ``• item`` in the stored ``text``, so the
    regex path leaks bullet characters into docs (visible today in
    ``03-cw-mpu.md``). The tree says ``rich_text_list`` and leaves the marker
    to us, so the local dialect survives the round trip.
    """
    ordered = el.get('style') == 'ordered'
    pad = _LIST_INDENT * el.get('indent', 0)
    lines = []
    for i, item in enumerate(el.get('elements', []), start=1):
        marker = f'{i}. ' if ordered else '- '
        lines.append(f'{pad}{marker}{_section(item)}')
    return '\n'.join(lines)


_RENDERERS = {
    'rich_text_section': _section,
    'rich_text_preformatted': _preformatted,
    'rich_text_quote': _quote,
    'rich_text_list': _list,
}


def render(blocks: list[dict] | None) -> str | None:
    """Markdown for the message's ``rich_text`` block, or None if it has none.

    None is the signal to fall back to `mrkdwn.to_markdown`, so a message shape
    we don't recognize degrades to the old path instead of losing content.
    """
    for block in blocks or []:
        if block.get('type') != 'rich_text':
            continue
        out: list[str] = []
        for el in block.get('elements', []):
            kind = el.get('type')
            # A list or fenced block starts a line of its own, and Slack emits
            # a nested list as a *sibling* block rather than a child — so
            # without this every sub-item lands glued to its parent's line.
            if kind in _BLOCK_LEVEL and out and not out[-1].endswith('\n'):
                out.append('\n')
            renderer = _RENDERERS.get(kind)
            out.append(renderer(el) if renderer else _section(el))
        # Slack HTML-encodes on storage and the tree is no exception — a
        # permalink's `&cid=` arrives as `&amp;cid=` inside `url`, not just in
        # the flat `text`. Same order as `to_markdown`: entities, then emoji.
        return decode_emoji(decode_entities(''.join(out)))
    return None
