"""Thread files: ``NN-slug.md``, one per Slack thread in a session.

The session directory holds one markdown file per thread, named with a
two-digit ordering prefix and the thread's slug::

    01-cw-quickwins.md
    02-cw-summary.md
    03-cw-mpu.md

Rationale (see ``specs/per-thread-model.md``): with N threads in one doc, the
gist's git history interleaves unrelated drafts, so a commit reads as "the doc
changed" rather than "*this message* went v2→v3". Since the gist history is the
artifact these sessions are collected for, that conflation is the costly part.
One file per thread makes per-file history exactly that message's revision
trajectory.

The ``NN`` prefix gives deterministic ordering for the batch case (post six
messages in a known order) and costs nothing for the reply case. The ``slug``
is the thread's identity: it keys ``thrds.json``'s ``threads`` map, is stamped
into each posted message's Slack metadata, and is the target of ``[text](#slug)``
cross-references from sibling threads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# `01-cw-quickwins.md` → (1, 'cw-quickwins'). The slug charset matches
# `md._HEADER_RE`'s, so slugs that were legal as `=== slug` stay legal as
# filenames (and vice versa) — migration never has to rewrite a slug.
THREAD_FILE_RE = re.compile(r'^(\d{2})-([a-zA-Z0-9_-]+)\.md$')

INDEX_WIDTH = 2


@dataclass(frozen=True, order=True)
class ThreadFile:
    """One ``NN-slug.md`` in a session directory.

    Ordered by ``(index, slug)`` so a sorted list of these is the doc order —
    which is the order threads get posted in on a batch push.
    """
    index: int
    slug: str
    path: Path

    @property
    def name(self) -> str:
        """The canonical basename for this (index, slug) pair."""
        return thread_filename(self.index, self.slug)


def thread_filename(index: int, slug: str) -> str:
    """Canonical ``NN-slug.md`` basename for ``index`` and ``slug``.

    Raises for an index that doesn't fit the fixed width, rather than emitting
    a name that would sort wrongly against its siblings (``100-x.md`` sorting
    before ``99-x.md``).
    """
    if index < 0:
        raise ValueError(f"Thread index must be non-negative, got {index}")
    if len(str(index)) > INDEX_WIDTH:
        raise ValueError(
            f"Thread index {index} exceeds {INDEX_WIDTH} digits; "
            f"a session with >{10 ** INDEX_WIDTH - 1} threads needs a wider prefix"
        )
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', slug):
        raise ValueError(f"Invalid thread slug {slug!r}: expected [a-zA-Z0-9_-]+")
    return f"{index:0{INDEX_WIDTH}d}-{slug}.md"


def parse_thread_filename(name: str) -> tuple[int, str] | None:
    """Parse ``NN-slug.md`` → ``(index, slug)``; None if it isn't a thread file.

    Returning None (rather than raising) lets callers scan a session directory
    that also holds ``thrds.json``, downloaded emoji, and a README without
    having to pre-filter.
    """
    m = THREAD_FILE_RE.match(name)
    if m is None:
        return None
    return (int(m.group(1)), m.group(2))


def thread_files(session_dir: Path | str = '.') -> list[ThreadFile]:
    """All ``NN-slug.md`` files in ``session_dir``, sorted by (index, slug).

    Raises on a duplicate slug across two files (e.g. ``01-foo.md`` and
    ``02-foo.md``) — slugs are the thread identity used by state, metadata,
    and cross-references, so a collision is unrecoverable ambiguity rather
    than something to resolve by picking one.
    """
    d = Path(session_dir)
    found: list[ThreadFile] = []
    by_slug: dict[str, ThreadFile] = {}
    for p in sorted(d.iterdir()) if d.is_dir() else []:
        if not p.is_file():
            continue
        parsed = parse_thread_filename(p.name)
        if parsed is None:
            continue
        index, slug = parsed
        tf = ThreadFile(index=index, slug=slug, path=p)
        if slug in by_slug:
            raise ValueError(
                f"Duplicate thread slug {slug!r}: {by_slug[slug].path.name} and {p.name}"
            )
        by_slug[slug] = tf
        found.append(tf)
    return sorted(found)


def next_index(files: list[ThreadFile]) -> int:
    """The index a newly-added thread should take: one past the highest in use.

    Gaps are preserved rather than compacted — renumbering would rename files,
    and a rename breaks the per-file git history that is the whole point of
    the one-file-per-thread layout.
    """
    return max((f.index for f in files), default=0) + 1
