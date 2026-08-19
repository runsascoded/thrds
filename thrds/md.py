"""Parse and serialize the thread markdown format.

Two layouts share this module:

- **per-thread files** (current; see ``specs/per-thread-model.md``) — one
  ``NN-slug.md`` per thread, parsed by :func:`parse_thread` /
  :func:`serialize_thread`. The slug comes from the filename; ``+++``
  separates replies; ``===`` is rejected.
- **single multi-thread doc** (legacy) — one ``.md`` holding every thread,
  parsed by :func:`parse_doc` / :func:`serialize_doc`, where ``===`` starts
  each thread. Retained to read pre-migration sessions and to drive
  ``thrds slack migrate``, which splits such a doc into per-thread files.

Legacy format
-------------
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

import difflib
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


@dataclass
class ParsedThread:
    """One thread parsed from its own ``NN-slug.md`` file, plus frontmatter.

    The per-thread counterpart to :class:`ParsedDoc`. The thread's slug comes
    from the *filename*, not from any in-file marker — see
    :mod:`thrds.threadfile`.
    """
    thread: DocThread
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
        start = i
        while i < len(lines):
            is_next_header, _ = _match_header(lines[i])
            if is_next_header:
                break
            i += 1
        threads.append(DocThread(messages=_split_messages(lines[start:i], slug), slug=slug))

    return ParsedDoc(doc=Doc(threads=threads, preamble=preamble), frontmatter=frontmatter)


def _split_messages(lines: list[str], label: str | None) -> list[DocMessage]:
    """Split ``lines`` on ``+++`` into OP + replies, validating non-emptiness.

    ``label`` names the thread in error messages — its slug, or ``None`` for a
    thread that has none.
    """
    current: list[str] = []
    current_author: str | None = None  # OP is always ours
    messages: list[DocMessage] = []
    for line in lines:
        reply_m = _REPLY_RE.match(line)
        if reply_m:
            messages.append(DocMessage(content='\n'.join(current).strip(), author=current_author))
            current = []
            current_author = reply_m.group(1)
            continue
        current.append(line)
    messages.append(DocMessage(content='\n'.join(current).strip(), author=current_author))
    if not messages[0].content:
        raise ValueError(f"Thread {label!r}: OP (first message) is empty")
    for j, m in enumerate(messages[1:], start=1):
        if not m.content:
            raise ValueError(f"Thread {label!r}: reply {j} is empty")
    return messages


def parse_thread(text: str, slug: str | None = None) -> ParsedThread:
    """Parse one thread's ``.md`` file into a `DocThread` + its frontmatter.

    The per-thread-file counterpart to :func:`parse_doc`: the whole file is a
    single thread (OP plus ``+++``-separated replies), and the slug comes from
    the filename rather than a ``=== slug`` header. A stray ``===`` is an
    error — it's the old multi-thread-per-file syntax, and silently accepting
    it would let a doc that means several threads post as one.

    Round-trip guarantee: ``parse_thread(serialize_thread(t, fm)) == (t, fm)``.
    """
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

    body = lines[i:]
    for n, line in enumerate(body):
        is_header, _ = _match_header(line)
        if is_header:
            raise ValueError(
                f"Thread {slug!r}: unexpected `===` at line {i + n}: one file is one thread; "
                f"`===` (multi-thread-per-file) was retired — split into `NN-slug.md` files "
                f"via `thrds slack migrate`"
            )

    return ParsedThread(
        thread=DocThread(messages=_split_messages(body, slug), slug=slug),
        frontmatter=frontmatter,
    )


def _render_frontmatter(frontmatter: Frontmatter | None) -> list[str]:
    """Frontmatter block as serialized parts (empty when absent or all-None)."""
    if frontmatter is None:
        return []
    fm_items = [
        (k, getattr(frontmatter, k))
        for k in _KNOWN_FRONTMATTER_KEYS
        if getattr(frontmatter, k) is not None
    ]
    if not fm_items:
        return []
    fm_body = '\n'.join(f'{k}: {v}' for k, v in fm_items)
    return [f'{_FRONTMATTER_DELIM}\n{fm_body}\n{_FRONTMATTER_DELIM}']


def serialize_thread(thread: DocThread, frontmatter: Frontmatter | None = None) -> str:
    """Serialize one `DocThread` to canonical per-thread `.md`.

    No ``===`` header — the slug lives in the filename. Canonical form matches
    :func:`serialize_doc`: single blank line between sections, trailing
    newline, no leading blank lines.
    """
    parts: list[str] = _render_frontmatter(frontmatter)

    for i, msg in enumerate(thread.messages):
        if i > 0:
            parts.append(f'+++ @{msg.author}' if msg.author else '+++')
        if i == 0 and msg.author is not None:
            raise ValueError(
                f"Thread {thread.slug!r}: OP author must be None (top-level = ours), "
                f"got {msg.author!r}"
            )
        parts.append(msg.content.strip())

    return '\n\n'.join(parts) + '\n'


def diff_texts(
    a: str,
    b: str,
    from_label: str,
    to_label: str,
    context: int = 3,
) -> str:
    """Unified diff of two `.md` texts; empty string when they're identical.

    The per-thread counterpart to :func:`diff_docs` compares a working-tree
    file byte-for-byte against what `pull` would write, so it needs a diff that
    does *not* canonicalize its left side — non-canonical local formatting is a
    change `pull` would make, and hiding it would misreport.
    """
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=from_label,
        tofile=to_label,
        n=context,
    ))


def diff_docs(
    a: Doc,
    b: Doc,
    from_label: str = 'local',
    to_label: str = 'slack',
    context: int = 3,
) -> str:
    """Unified diff of two Docs as canonical `.md` text.

    Both sides are serialized via `serialize_doc` (no frontmatter — the diff
    is about doc content, not per-source metadata), so formatting variants
    that round-trip to the same canonical form don't leak into the diff.
    Returns an empty string when the two Docs are canonically identical.
    """
    return diff_texts(
        serialize_doc(a),
        serialize_doc(b),
        from_label=from_label,
        to_label=to_label,
        context=context,
    )


def serialize_doc(doc: Doc, frontmatter: Frontmatter | None = None) -> str:
    """Serialize a Doc (+ optional frontmatter) to canonical `.md`.

    Canonical form: single blank line between every top-level section,
    trailing newline, no leading blank lines. This is the fixed point of
    parse ∘ serialize.
    """
    parts: list[str] = _render_frontmatter(frontmatter)

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
