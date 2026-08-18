"""Staging-only chrome: a one-line footer rendered into the message text.

**Why the text and not `blocks`.** Chrome first shipped as `context` blocks
wrapping a `section` body, which kept it structurally unable to leak into a
doc. But Slack removes the *Edit message* affordance from any message carrying
blocks — and a staging channel you can't edit in defeats the point of having
one. Worse, it took the affordance this feature most wanted to enable: pointing
a draft at a different channel by editing the line that says where it's going.

So chrome is the last line of the message text, appended after md→mrkdwn
conversion and stripped before the reverse, so neither direction of the
converter ever sees it. Two things replace the structural guarantee blocks gave:

* :func:`split` strips the footer on pull, so it can't round-trip into a doc.
* `promote_thread` refuses to post a body that still carries one, so a strip
  that somehow failed open fails closed at the only boundary that matters.

**Shape.** One line, ` · `-joined, no icons — it sits in the message body at
body size, so it earns its prominence by being short::

    → #oa-amazon-trainium · posted · 01-mfu.md
    → (#marin-alerts) · posted · 02-cw-summary.md

In the second, the arrow itself links to the message being replied to: the ts
is what a machine needs and a human never reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEP = ' · '
ARROW = '→'
GIST_HOST = 'https://gist.github.com'

# `<#C0ABCDEF>` or `<#C0ABCDEF|name>` — Slack's channel mention.
_CHANNEL = r'<#(?P<{name}>[A-Z0-9]+)(?:\|[^>]*)?>'

# A Slack permalink's ts lives in its `/pXXXXXXXXXXYYYYYY` path segment.
_PERMALINK_TS = re.compile(r'/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d{6,})')

# The four segment shapes. Anything else in a footer means it isn't one.
_TOP = re.compile(rf'^{ARROW} {_CHANNEL.format(name="channel")}$')
_REPLY = re.compile(
    rf'^<(?P<url>https?://[^>|]+)\|{ARROW}> \({_CHANNEL.format(name="channel")}\)$'
)
# Lenient authoring form: `→ <permalink>`. Slack auto-links a pasted URL, so
# this is what retargeting a draft at someone's message looks like by hand.
_PASTED = re.compile(rf'^{ARROW} <?(?P<url>https?://[^>\s|]+)>?$')
_POSTED = re.compile(r'^<(?P<url>https?://[^>|]+)\|posted>$')
_GIST = re.compile(rf'^<(?P<url>{re.escape(GIST_HOST)}/[^>|]+)\|(?P<filename>[^>]+)>$')


def gist_file_url(gist_id: str, filename: str) -> str:
    """Deep-link to ``filename`` within a gist.

    GitHub anchors each file as ``#file-<name>`` with every non-alphanumeric
    run folded to a single dash, lowercased — so ``01-mfu.md`` is
    ``#file-01-mfu-md``. Without the fragment a six-thread gist opens at the
    top and the reader hunts.
    """
    anchor = re.sub(r'[^a-z0-9]+', '-', filename.lower()).strip('-')
    return f'{GIST_HOST}/{gist_id}#file-{anchor}'


@dataclass(frozen=True)
class ChromeEdit:
    """What a staged message's footer says that its local state doesn't.

    ``target`` is applied to state by the caller (retargeting is the whole
    point of an editable footer). ``renamed_to`` is only *reported*: renaming
    a thread file is how you reorder threads, but it breaks the per-file git
    history the layout exists to give, so it's a human's call.
    """
    slug: str
    target_was: "object | None" = None
    target_now: "object | None" = None
    renamed_to: str | None = None


@dataclass(frozen=True)
class Chrome:
    """A parsed footer: what it says about where this thread is bound."""
    channel: str | None = None
    thread_ts: str | None = None
    filename: str | None = None
    posted_url: str | None = None


def _ts_from_permalink(url: str) -> tuple[str | None, str | None]:
    """``(channel, ts)`` from a Slack permalink; ``(None, None)`` if it isn't one."""
    m = _PERMALINK_TS.search(url)
    if m is None:
        return None, None
    digits = m.group('ts')
    return m.group('channel'), f'{digits[:-6]}.{digits[-6:]}'


def render(
    *,
    channel: str | None,
    thread_ts: str | None,
    target_url: str | None,
    posted_url: str | None,
    gist_id: str | None,
    filename: str | None,
) -> str | None:
    """The footer line, or None when nothing is worth saying.

    ``target_url`` is the permalink of the message being replied to; without
    it a reply target degrades to the plain channel form rather than rendering
    a bare ts nobody reads.
    """
    parts: list[str] = []
    if channel is not None:
        if thread_ts is not None and target_url is not None:
            parts.append(f'<{target_url}|{ARROW}> (<#{channel}>)')
        else:
            parts.append(f'{ARROW} <#{channel}>')
    if posted_url is not None:
        parts.append(f'<{posted_url}|posted>')
    if gist_id is not None and filename is not None:
        parts.append(f'<{gist_file_url(gist_id, filename)}|{filename}>')
    return SEP.join(parts) if parts else None


def parse(line: str) -> Chrome | None:
    """Parse a footer line, or None if ``line`` isn't one.

    Every segment must match a known shape and at least one must name a
    destination or a gist file — a body's last line satisfying all of that by
    accident is not a case worth trading correctness for.
    """
    chrome = Chrome()
    anchored = False
    for i, segment in enumerate(line.split(SEP)):
        m = _TOP.match(segment)
        if m is not None and i == 0:
            chrome, anchored = Chrome(**{**vars(chrome), 'channel': m['channel']}), True
            continue
        m = _REPLY.match(segment)
        if m is not None and i == 0:
            _, ts = _ts_from_permalink(m['url'])
            chrome = Chrome(**{**vars(chrome), 'channel': m['channel'], 'thread_ts': ts})
            anchored = True
            continue
        m = _PASTED.match(segment)
        if m is not None and i == 0:
            channel, ts = _ts_from_permalink(m['url'])
            if channel is None:
                return None
            chrome = Chrome(**{**vars(chrome), 'channel': channel, 'thread_ts': ts})
            anchored = True
            continue
        m = _POSTED.match(segment)
        if m is not None:
            chrome = Chrome(**{**vars(chrome), 'posted_url': m['url']})
            continue
        m = _GIST.match(segment)
        if m is not None:
            chrome = Chrome(**{**vars(chrome), 'filename': m['filename']})
            anchored = True
            continue
        return None
    return chrome if anchored else None


def split(text: str) -> tuple[str, Chrome | None]:
    """``(body, chrome)`` — strip a trailing footer off raw Slack text.

    Operates on the wire text, before ``to_markdown``, mirroring `render`
    running after ``to_slack``. Text with no footer comes back untouched, so
    this is safe to call on every message including other people's.
    """
    stripped = text.rstrip('\n')
    head, sep, last = stripped.rpartition('\n')
    if not sep:
        return text, None
    chrome = parse(last.strip())
    if chrome is None:
        return text, None
    return head.rstrip('\n'), chrome


def has_chrome(text: str) -> bool:
    """Whether ``text`` still carries a footer — the promote-time guard."""
    return split(text)[1] is not None
