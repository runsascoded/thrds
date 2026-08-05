"""Parse and serialize the multi-thread doc markdown format.

Format
------
Two block-level delimiters split the document into two levels:

- ``=== slug``  starts a new top-level thread (its OP + zero or more replies).
  The ``slug`` is optional; when present it makes the thread targetable by
  ``[text](#slug)`` cross-references from other threads.
- ``+++``       starts a reply within the current top-level thread.

Text before the first ``===`` is the ``preamble`` — a bare top-level message
with no replies. Optional YAML frontmatter (``---`` delimited) at the very
top of the document carries per-doc metadata: ``channel``, ``thread_ts``,
``session_id``.

``---`` is deliberately NOT reused as a section delimiter: it collides with
YAML frontmatter and Markdown ``<hr>``, and the two-level structure needs
two markers anyway (see ``specs/multi-thread-posts-and-capture.md``).

Round-trip guarantee: ``parse_doc(serialize_doc(doc, fm)) == (doc, fm)``.
Message bodies are strip()'d — surrounding whitespace is not part of the
message. Internal blank lines (paragraph breaks within a message) are
preserved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .doc import Doc, DocMessage, DocThread, Frontmatter


_HEADER_RE = re.compile(r'^===(?:[ \t]+([a-zA-Z0-9_-]+))?[ \t]*$')
# `+++`               → reply from us (author=None)
# `+++ @alice`        → reply from `alice`; foreign, sync leaves it alone
_REPLY_RE = re.compile(r'^\+\+\+(?:[ \t]+@([a-zA-Z0-9._-]+))?[ \t]*$')
_FRONTMATTER_DELIM = '---'
_KNOWN_FRONTMATTER_KEYS = ('channel', 'thread_ts', 'session_id')


@dataclass
class ParsedDoc:
    """A doc parsed from its .md source, alongside its YAML frontmatter."""
    doc: Doc
    frontmatter: Frontmatter


def _match_header(line: str) -> tuple[bool, str | None]:
    """Return (is_header, slug). slug is None for a bare ``===``."""
    m = _HEADER_RE.match(line)
    if m is None:
        return (False, None)
    return (True, m.group(1))


def _parse_frontmatter_body(body: str) -> Frontmatter:
    """Parse ``key: value`` lines into a Frontmatter.

    Deliberately minimal — no nested structures, no list values, no quoting.
    Bump to PyYAML if we ever need more shape than string scalars.
    """
    data: dict[str, str] = {}
    for i, line in enumerate(body.split('\n')):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if ':' not in stripped:
            raise ValueError(f"Malformed frontmatter line {i}: {line!r}")
        k, _, v = stripped.partition(':')
        data[k.strip()] = v.strip()
    unknown = set(data) - set(_KNOWN_FRONTMATTER_KEYS)
    if unknown:
        raise ValueError(f"Unknown frontmatter keys: {sorted(unknown)}")
    return Frontmatter(**data)


def parse_doc(text: str) -> ParsedDoc:
    """Parse a `.md` doc into a `Doc` + its frontmatter."""
    lines = text.split('\n')
    i = 0
    frontmatter = Frontmatter()

    if i < len(lines) and lines[i].strip() == _FRONTMATTER_DELIM:
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == _FRONTMATTER_DELIM), None)
        if end is None:
            raise ValueError("Frontmatter opened with `---` but no closing delimiter found")
        frontmatter = _parse_frontmatter_body('\n'.join(lines[i + 1:end]))
        i = end + 1

    while i < len(lines) and not lines[i].strip():
        i += 1

    preamble_lines: list[str] = []
    while i < len(lines):
        is_header, _ = _match_header(lines[i])
        if is_header:
            break
        preamble_lines.append(lines[i])
        i += 1
    preamble_text = '\n'.join(preamble_lines).strip()
    preamble = preamble_text or None

    threads: list[DocThread] = []
    seen_slugs: set[str] = set()
    while i < len(lines):
        is_header, slug = _match_header(lines[i])
        if not is_header:
            raise ValueError(f"Expected `===` thread header at line {i}, got: {lines[i]!r}")
        if slug is not None:
            if slug in seen_slugs:
                raise ValueError(f"Duplicate thread slug: {slug!r}")
            seen_slugs.add(slug)
        i += 1
        current: list[str] = []
        current_author: str | None = None  # OP is always ours
        messages: list[DocMessage] = []
        while i < len(lines):
            is_next_header, _ = _match_header(lines[i])
            if is_next_header:
                break
            reply_m = _REPLY_RE.match(lines[i])
            if reply_m:
                messages.append(DocMessage(content='\n'.join(current).strip(), author=current_author))
                current = []
                current_author = reply_m.group(1)
                i += 1
                continue
            current.append(lines[i])
            i += 1
        messages.append(DocMessage(content='\n'.join(current).strip(), author=current_author))
        if not messages[0].content:
            raise ValueError(f"Thread {slug!r}: OP (first message) is empty")
        for j, m in enumerate(messages[1:], start=1):
            if not m.content:
                raise ValueError(f"Thread {slug!r}: reply {j} is empty")
        threads.append(DocThread(messages=messages, slug=slug))

    return ParsedDoc(doc=Doc(threads=threads, preamble=preamble), frontmatter=frontmatter)


def serialize_doc(doc: Doc, frontmatter: Frontmatter | None = None) -> str:
    """Serialize a Doc (+ optional frontmatter) to canonical `.md`.

    Canonical form: single blank line between every top-level section,
    trailing newline, no leading blank lines. This is the fixed point of
    parse ∘ serialize.
    """
    parts: list[str] = []

    if frontmatter is not None:
        fm_items = [
            (k, getattr(frontmatter, k))
            for k in _KNOWN_FRONTMATTER_KEYS
            if getattr(frontmatter, k) is not None
        ]
        if fm_items:
            fm_body = '\n'.join(f'{k}: {v}' for k, v in fm_items)
            parts.append(f'{_FRONTMATTER_DELIM}\n{fm_body}\n{_FRONTMATTER_DELIM}')

    if doc.preamble:
        parts.append(doc.preamble.strip())

    for thread in doc.threads:
        header = f'=== {thread.slug}' if thread.slug else '==='
        parts.append(header)
        for i, msg in enumerate(thread.messages):
            if i > 0:
                parts.append(f'+++ @{msg.author}' if msg.author else '+++')
            if i == 0 and msg.author is not None:
                raise ValueError(f"Thread {thread.slug!r}: OP author must be None (top-level = ours), got {msg.author!r}")
            parts.append(msg.content.strip())

    return '\n\n'.join(parts) + '\n'
