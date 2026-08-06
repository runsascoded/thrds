"""Markdown → Slack mrkdwn conversion, applied at post/edit time.

The local doc format is CommonMark-ish (readable in any md editor, on GitHub,
etc.); Slack has its own dialect. This module bridges the two, applied at the
last mile — inside `SlackClient.post()` / `SlackClient.edit()` — so that
higher-level code (`sync_doc_*`, ref resolution, cross-thread links) can stay
in markdown throughout and let a single choke point handle the conversion.

Handled today:

- ``[text](url)`` → ``<url|text>``. Slack's link format. This is the most
  common breakage: without it, the URL still auto-linkifies but ``[text](``
  and the trailing ``)`` render as dangling literals around it.
- ``**bold**`` → ``*bold*``. Slack uses single-asterisk bold; double-asterisk
  renders literally.
- ``*italic*`` → ``_italic_``. Slack's single-asterisk means bold.
- Standard emoji (``:left_right_arrow:`` etc.): unicode ↔ shortcode via the
  ``emoji`` package on pull. Slack HTML-encodes unicode emoji into shortcode
  form on storage, so this closes the pull-side roundtrip. Custom Slack
  workspace emoji (``:claude:``, ``:trainium:`` etc.) pass through as
  ``:name:`` — Slack round-trips them literally.

Intentionally not handled:

- Backticks: same in both.
- Bulleted lists (``- item``): Slack renders ``-`` bullets fine as-is.
- ``~~strike~~`` → ``~strike~``: not used in current docs; add on demand.
"""
from __future__ import annotations

import re

import emoji

# `[text](url)`. Non-greedy on text so `[a](b) and [c](d)` stays two links.
# Text may contain any char except `]`; url may contain any char except `)`.
_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# `**bold**`. Non-greedy on inner content; disallow embedded `*` to avoid
# eating triple-asterisk or mixed emphasis edge cases.
_MD_BOLD = re.compile(r'\*\*([^*]+)\*\*')

# `*italic*` (single-asterisk, NOT part of `**bold**`). Guards:
#
# - Not adjacent to `*` (would be `**bold**`).
# - Not adjacent to a word char on either side (else `2*4*8` becomes italic).
# - Inner content is non-empty, no `*` or newlines, and doesn't start/end with
#   whitespace (so `word * word` isn't caught).
_MD_ITALIC = re.compile(
    r'(?<![*\w])\*(?!\*)([^*\s\n](?:[^*\n]*[^*\s\n])?)\*(?![*\w])'
)


def to_slack(text: str) -> str:
    """Convert local markdown formatting to Slack mrkdwn.

    Order matters:

    1. Links first — a link inside bold gets rewritten before its enclosing
       ``**...**`` is rewritten in step 3, so the link isn't damaged.
    2. Italic BEFORE bold — the ``**bold**`` rewrite emits ``*bold*`` (single
       asterisks), which would otherwise be re-matched by the italic pattern.
       Doing italic first, using ``(?<!\\*)`` guards, keeps the two disjoint.
    3. Bold last.
    """
    text = _MD_LINK.sub(r'<\2|\1>', text)
    text = _MD_ITALIC.sub(r'_\1_', text)
    text = _MD_BOLD.sub(r'*\1*', text)
    return text


# `<url|text>`. Only URL-shaped first parts (http/https) — avoids swallowing
# Slack's `<@Uxxx>`, `<#Cxxx>`, `<!here>` mentions, which don't carry `|`
# anyway but we're defensive. `text` matches anything but `>`.
_SLACK_LINK = re.compile(r'<(https?://[^>|]+)\|([^>]+)>')

# `*bold*` (Slack side). Word-boundary guards prevent identifier-eating like
# `foo *bar* baz` fine, but `a*b*c` (arithmetic) not matched. Newlines and
# nested `*` disallowed inside.
_SLACK_BOLD = re.compile(
    r'(?<![*\w])\*([^\s*\n](?:[^*\n]*[^\s*\n])?)\*(?![*\w])'
)

# `_italic_` (Slack side). Word-boundary guards are LOAD-BEARING here: without
# them, `thread_ts=1&cid=X thread_ts=2` gets its two `_`s glued into an italic
# span across half the URL. Same shape as `*bold*` otherwise.
_SLACK_ITALIC = re.compile(
    r'(?<!\w)_([^\s_\n](?:[^_\n]*[^\s_\n])?)_(?!\w)'
)


# Variation selectors — U+FE0E (text style) and U+FE0F (emoji style).
# `emoji.emojize` re-adds these on any emoji that declares them intrinsic
# (e.g. `:left_right_arrow:` gets VS-16 back), but our local doc convention
# is bare codepoints, so a naive roundtrip drifts by 1 char per emoji. Strip.
_VS_16 = re.compile(r'[︎️]')


def to_markdown(text: str) -> str:
    """Convert Slack mrkdwn back to local markdown format.

    Inverse of `to_slack`:

    - ``<url|text>`` → ``[text](url)`` (URL-scheme-guarded so `<@Uxxx>` etc.
      pass through untouched).
    - ``*bold*`` → ``**bold**``. Slack's single-star always means bold.
    - ``_italic_`` → ``*italic*``. Local convention uses single-star italic
      (which we send as ``_italic_`` on the wire; this is the reverse).
    - ``:name:`` (standard emoji only) → unicode via ``emoji.emojize``.
      Variation selectors stripped for a bare-codepoint roundtrip. Custom
      workspace emoji (``:claude:`` etc.) don't match any standard alias,
      so ``emojize`` leaves them untouched — Slack round-trips those as
      literal text anyway.
    - HTML entities Slack HTML-encodes on storage: ``&amp;`` → ``&``,
      ``&lt;`` → ``<``, ``&gt;`` → ``>``.

    Entity decode runs AFTER link rewriting so a `&` inside a URL query
    string stays inside the resulting markdown-link `(...)`.
    """
    text = _SLACK_LINK.sub(r'[\2](\1)', text)
    text = _SLACK_BOLD.sub(r'**\1**', text)
    text = _SLACK_ITALIC.sub(r'*\1*', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = _VS_16.sub('', emoji.emojize(text, language='alias'))
    return text
