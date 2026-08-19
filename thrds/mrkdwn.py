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
  form on storage, so this closes the pull-side roundtrip.
- Custom Slack workspace emoji (``:claude:`` etc.): rendered inline as
  ``![:name:](emoji-name.ext)`` in the local doc — the image file lives at
  session root (gists reject subdirs). ``to_slack`` collapses that back to
  ``:name:`` for the wire. Pull-side download + substitution lives in
  ``slack.py`` (needs Slack API + local FS access, so can't stay pure here);
  ``substitute_custom_emoji`` is the pure text-substitution half.

Intentionally not handled:

- Backticks: same in both.
- Bulleted lists (``- item``): Slack renders ``-`` bullets fine as-is.
- ``~~strike~~`` → ``~strike~``: not used in current docs; add on demand.
"""
from __future__ import annotations

import re

import emoji

# `![:name:](anything)` — an emoji image the pull step wrote into the doc.
# Alt text must be `:name:` (that's how we recognize an emoji image vs. a
# regular image); URL body isn't inspected here — `to_slack` only needs
# the emoji name. Slack chars for emoji names: `[a-z0-9_+-]`.
_MD_EMOJI_IMG = re.compile(r'!\[:([a-z0-9_+\-]+):\]\([^)]+\)')

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

    1. Emoji images first — ``![:name:](emoji-name.png)`` is a "special link"
       whose ``![…](…)`` shell would otherwise get consumed by the regular
       link rewrite in step 2.
    2. Links next — a link inside bold gets rewritten before its enclosing
       ``**...**`` is rewritten in step 4, so the link isn't damaged.
    3. Italic BEFORE bold — the ``**bold**`` rewrite emits ``*bold*`` (single
       asterisks), which would otherwise be re-matched by the italic pattern.
       Doing italic first, using ``(?<!\\*)`` guards, keeps the two disjoint.
    4. Bold last.
    """
    text = _MD_EMOJI_IMG.sub(r':\1:', text)
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


# A fence is a run of 3+ backticks. Slack's mrkdwn treats ```` ```x ```` as a
# block opener; CommonMark reads the `x` as an info string and drops it from
# the rendered content.
_FENCE = re.compile(r'`{3,}')

# An info string is a language tag: one token, no whitespace. A first line
# with a space in it is content that Slack simply didn't wrap.
_INFO_STRING = re.compile(r'\S+')


def normalize_fences(text: str) -> str:
    """Re-inflate Slack's compact code fences into valid CommonMark.

    Slack lets a fenced block open and close flush against its content
    (```` ```7.53 TiB … 0.00 TiB``` ````) and renders it correctly. CommonMark
    doesn't: the opening fence eats the first line as an info string and the
    closing fence, not starting a line, is shown literally. So a block composed
    in the Slack UI arrives readable in Slack and broken everywhere the gist is
    read. See `specs/pull-fence-normalization.md`.

    Inserts newlines only — content is preserved byte-for-byte, including
    leading whitespace, since in a code block that indentation is data.

    A first line that *looks* like an info string (one token, no whitespace,
    with real content on later lines) is left attached, because ```` ```python ````
    round-tripped from a local doc has to come back unchanged. That leaves one
    irreducible ambiguity: a Slack-composed block whose first line is a single
    bare word is indistinguishable from a language tag, and stays attached.

    Unpaired trailing fences are left alone rather than guessed at.
    """
    fences = list(_FENCE.finditer(text))
    if len(fences) < 2:
        return text
    out: list[str] = []
    prev_end = 0
    for i in range(0, len(fences) - 1, 2):
        opener, closer = fences[i], fences[i + 1]
        out.append(text[prev_end:opener.end()])
        body = text[opener.end():closer.start()]
        if body:
            first_line, sep, _ = body.partition('\n')
            is_info_string = bool(sep) and bool(_INFO_STRING.fullmatch(first_line))
            if not body.startswith('\n') and not is_info_string:
                out.append('\n')
            if not body.endswith('\n'):
                body += '\n'
        out.append(body)
        prev_end = closer.start()
    out.append(text[prev_end:])
    return ''.join(out)


def decode_entities(text: str) -> str:
    """Undo the HTML encoding Slack applies on storage.

    Split out because chrome comparison needs it without the rest of the
    mrkdwn→markdown rewrite: a permalink's ``&cid=`` comes back ``&amp;cid=``,
    and comparing that against a freshly-rendered footer would report drift on
    every push.
    """
    return text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


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
    - Compact code fences are re-inflated to valid CommonMark
      (see `normalize_fences`).
    - HTML entities Slack HTML-encodes on storage: ``&amp;`` → ``&``,
      ``&lt;`` → ``<``, ``&gt;`` → ``>``.

    Entity decode runs AFTER link rewriting so a `&` inside a URL query
    string stays inside the resulting markdown-link `(...)`.
    """
    text = normalize_fences(text)
    text = _SLACK_LINK.sub(r'[\2](\1)', text)
    text = _SLACK_BOLD.sub(r'**\1**', text)
    text = _SLACK_ITALIC.sub(r'*\1*', text)
    text = decode_entities(text)
    text = _VS_16.sub('', emoji.emojize(text, language='alias'))
    return text


# `:name:` custom emoji shortcode — anything the standard emoji lib doesn't
# know about. Matches names Slack allows (a-z, 0-9, _, +, -). Negative
# lookbehind `(?<!!\[)` skips `:name:` already wrapped as `![:name:](...)`
# — needed for `substitute_custom_emoji` to be idempotent.
_SHORTCODE = re.compile(r'(?<!!\[):([a-z0-9_+\-]+):')


def find_custom_shortcodes(text: str) -> set[str]:
    """Names of `:name:` shortcodes in `text` that are NOT standard emoji.

    Probe per name: if ``emoji.emojize(':name:')`` still returns ``:name:``,
    the name isn't a standard alias — treat it as a workspace-custom emoji.
    """
    return {
        name for name in set(_SHORTCODE.findall(text))
        if emoji.emojize(f':{name}:', language='alias') == f':{name}:'
    }


def substitute_custom_emoji(text: str, name_to_filename: dict[str, str]) -> str:
    """Rewrite `:name:` → `![:name:](filename)` for each mapped name.

    Idempotent: already-substituted `![:name:](...)` won't match `:name:`
    because `_SHORTCODE` matches only bare `:foo:` (no adjacent `]`).
    """
    def repl(m: re.Match) -> str:
        name = m.group(1)
        fn = name_to_filename.get(name)
        return f'![:{name}:]({fn})' if fn else m.group(0)
    return _SHORTCODE.sub(repl, text)
