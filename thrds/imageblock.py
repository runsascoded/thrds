"""Standalone image lines ↔ Slack Block Kit ``image`` blocks.

`specs/editable-image-blocks.md`: a trailing standalone ``![alt](url)`` line
in a message body becomes a Block Kit ``image`` block appended after the
message's ``section``. The point is a **live image**: an attached file
(``files.upload``) can't be edited, but an ``image`` block's ``image_url``
swaps via ``chat.update`` — so a chart/card at the top of a thread can
refresh in place as its underlying data changes.

Only a *trailing* run of image lines is lifted (see
:func:`split_trailing_images`); the read-back path appends reconstructed
image lines at the end of the body, so trailing-only is what keeps a converge
idempotent — a mid-message image would round-trip to a different doc and
re-edit forever.

Cache-busting: Slack caches images by URL and won't refetch changed bytes at
the same URL. ``![alt](url){bust}`` opts a line into a ``?thrds_bust=<token>``
query param, refreshed on each post/edit (a SKIP never touches it, so a no-op
converge stays a no-op). The param name is deliberately distinctive — a plain
``?v=`` would be indistinguishable from a caller-versioned URL on read-back,
and stripping *that* would corrupt it. Callers who version their own URLs
(``card.png?v=<scan>``) don't need ``{bust}``: the changed URL string is an
ordinary content diff.
"""
from __future__ import annotations

import re
import time
import warnings
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BUST_PARAM = 'thrds_bust'

# `![alt](url)` alone on its line, optional `{bust}` suffix. Alt may be empty
# (warned at block-build time); the url can't contain whitespace or `)`.
_IMAGE_LINE_RE = re.compile(
    r'^!\[(?P<alt>[^\]\n]*)\]\((?P<url>[^)\s]+)\)(?P<bust>\{bust\})?[ \t]*$'
)

# A custom-emoji image (`![:name:](name.png)`) is NOT an image block — it
# belongs to `mrkdwn.to_slack`, which collapses it to `:name:` on the wire.
_EMOJI_ALT_RE = re.compile(r'^:[a-z0-9_+\-]+:$')


@dataclass(frozen=True)
class ImageRef:
    """One image destined for (or recovered from) a Block Kit ``image`` block.

    ``url`` never carries the bust param — ``bust=True`` records that the
    wire URL gets a fresh ``?thrds_bust=<token>`` on each post/edit.
    """
    alt: str
    url: str
    bust: bool = False


def parse_image_line(line: str) -> ImageRef | None:
    """``![alt](url)`` / ``![alt](url){bust}`` alone on its line, else None."""
    m = _IMAGE_LINE_RE.match(line)
    if m is None or _EMOJI_ALT_RE.match(m.group('alt')):
        return None
    return ImageRef(alt=m.group('alt'), url=m.group('url'), bust=m.group('bust') is not None)


def image_line(ref: ImageRef) -> str:
    """The doc-side line for ``ref`` — inverse of :func:`parse_image_line`."""
    return f'![{ref.alt}]({ref.url})' + ('{bust}' if ref.bust else '')


def split_trailing_images(content: str) -> tuple[str, list[ImageRef]]:
    """``(body, images)`` — lift the trailing run of standalone image lines.

    The run may include blank lines between image lines; it ends at the first
    non-blank, non-image line scanning up from the bottom. An image line
    mid-message stays literal text (and renders as such).
    """
    lines = content.split('\n')
    refs: list[ImageRef] = []
    i = len(lines)
    while i > 0:
        line = lines[i - 1]
        if not line.strip():
            i -= 1
            continue
        ref = parse_image_line(line)
        if ref is None:
            break
        refs.append(ref)
        i -= 1
    if not refs:
        return content, []
    refs.reverse()
    return '\n'.join(lines[:i]).rstrip('\n'), refs


def bust_token() -> str:
    """Minute-resolution token: stable within a minute, fresh across runs.

    Re-derived, never stored — the spec left open whether the last token
    needs to live in ``thrds.yml``; it doesn't, because only POST/EDIT calls
    mint one, and those already imply a refetch is wanted.
    """
    return str(int(time.time() // 60))


def bust_url(url: str, token: str) -> str:
    """``url`` with ``?thrds_bust=<token>`` appended (replacing any prior)."""
    scheme, netloc, path, query, frag = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != BUST_PARAM]
    params.append((BUST_PARAM, token))
    return urlunsplit((scheme, netloc, path, urlencode(params), frag))


def strip_bust(url: str) -> tuple[str, bool]:
    """``(url without the bust param, whether one was present)``.

    A URL with no bust param comes back byte-identical (no re-encoding pass),
    so caller-versioned query strings survive the round-trip untouched.
    """
    scheme, netloc, path, query, frag = urlsplit(url)
    params = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in params if k != BUST_PARAM]
    if len(kept) == len(params):
        return url, False
    return urlunsplit((scheme, netloc, path, urlencode(kept), frag)), True


def to_block(ref: ImageRef, token: str | None = None) -> dict:
    """`ImageRef` → Block Kit ``image`` block dict.

    Empty alt goes out as-is (Slack requires the *field*, and substituting a
    placeholder would break read-back convergence) with a warning — fill it
    in for accessibility.
    """
    if not ref.alt:
        warnings.warn(f'image {ref.url} has empty alt text', stacklevel=2)
    url = bust_url(ref.url, token) if ref.bust and token is not None else ref.url
    return {'type': 'image', 'image_url': url, 'alt_text': ref.alt}


def from_block(block: dict) -> ImageRef | None:
    """Block Kit ``image`` block → `ImageRef`; None for any other block type.

    A ``thrds_bust`` param on the wire URL is stripped and recorded as
    ``bust=True``, so ``![alt](url){bust}`` round-trips through Slack.
    """
    if block.get('type') != 'image':
        return None
    url, bust = strip_bust(block.get('image_url', ''))
    return ImageRef(alt=block.get('alt_text', ''), url=url, bust=bust)
