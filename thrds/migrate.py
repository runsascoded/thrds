"""Convert a legacy single-doc session to the per-thread-file layout.

A legacy session has one ``.md`` holding every thread (``===``-separated), with
thread pointers split across ``staging_threads`` and
``prod_threads[channel][slug]``. The per-thread layout (see
``specs/per-thread-model.md``) has one ``NN-slug.md`` per thread and a single
``threads`` map carrying each thread's staging ts, destination, and state.

Migration is split into a **pure planning** step (:func:`plan_migration`, which
touches no files and is exhaustively testable) and an **apply** step
(:func:`apply_migration`). That split exists because the interesting failure
modes — a thread with no slug, a slug posted to two different prod channels, a
preamble colliding with a real thread's slug — are all detectable from the doc
and state alone, and should abort before anything on disk changes.

Content is preserved byte-for-byte: each thread's messages are re-serialized
through the same ``_split_messages`` / ``_render_frontmatter`` helpers that the
legacy doc parser uses, so a migrated file's body is exactly the bytes that
lived between its ``===`` header and the next one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .doc import Doc, DocMessage, DocThread, Frontmatter
from .md import serialize_thread
from .state import SessionState, ThreadEntry, ThreadTarget
from .threadfile import thread_filename, thread_files


# The preamble — a bare top-level message with no replies — becomes an ordinary
# thread at index 0, which retires its special-cased sync path entirely.
PREAMBLE_SLUG = 'preamble'
PREAMBLE_INDEX = 0


@dataclass
class MigratedThread:
    """One thread's planned destination file plus its new state entry."""
    index: int
    slug: str
    filename: str
    text: str
    entry: ThreadEntry


@dataclass
class MigrationPlan:
    """Everything migration will do, computed before touching disk."""
    session_slug: str
    doc_path: str
    threads: list[MigratedThread] = field(default_factory=list)

    @property
    def filenames(self) -> list[str]:
        return [t.filename for t in self.threads]

    @property
    def threads_map(self) -> dict[str, ThreadEntry]:
        return {t.slug: t.entry for t in self.threads}


def _prod_placement(state: SessionState, slug: str) -> tuple[str, str] | None:
    """``(channel, ts)`` where ``slug`` was posted to prod, or None if it wasn't.

    Raises when a slug was posted to more than one prod channel: the per-thread
    model gives each thread exactly one destination, so there is no faithful
    single target to migrate such a thread to. Better to stop and let a human
    decide than to silently drop one of the placements.
    """
    placements = [
        (channel, by_slug[slug])
        for channel, by_slug in sorted(state.prod_threads.items())
        if slug in by_slug
    ]
    if not placements:
        return None
    if len(placements) > 1:
        channels = ', '.join(c for c, _ in placements)
        raise ValueError(
            f"Thread {slug!r} was posted to multiple prod channels ({channels}); "
            f"the per-thread model allows one destination per thread — "
            f"split it into separate threads before migrating"
        )
    return placements[0]


def _entry_for(
    state: SessionState,
    slug: str,
    default_target: ThreadTarget | None,
) -> ThreadEntry:
    """Build the :class:`ThreadEntry` for ``slug`` from legacy state.

    A thread already posted to prod migrates as ``posted`` with its actual
    channel pinned as the target — that's a fact about where it went, not a
    default. Anything else migrates as ``draft`` and inherits
    ``default_target`` (which may be None, leaving it to the session-level
    ``prod_channel`` fallback in :meth:`SessionState.target_for`).
    """
    placement = _prod_placement(state, slug)
    if placement is not None:
        channel, posted_ts = placement
        return ThreadEntry(
            staging_ts=state.staging_threads.get(slug),
            target=ThreadTarget(channel=channel),
            state='posted',
            posted_ts=posted_ts,
        )
    return ThreadEntry(
        staging_ts=state.staging_threads.get(slug),
        target=default_target,
        state='draft',
    )


def plan_migration(
    doc: Doc,
    state: SessionState,
    doc_path: str,
    frontmatter: Frontmatter | None = None,
) -> MigrationPlan:
    """Plan the split of ``doc`` into per-thread files. Touches no files.

    Doc-level frontmatter ``channel`` / ``thread_ts`` translate directly into a
    default :class:`ThreadTarget` — in the legacy format those were a per-doc
    override of the session's destination, which is exactly what a per-thread
    target is, only now per thread.
    """
    session_slug = state.session_slug or Path(doc_path).stem

    default_target: ThreadTarget | None = None
    if frontmatter is not None and frontmatter.channel is not None:
        default_target = ThreadTarget(
            channel=frontmatter.channel,
            thread_ts=frontmatter.thread_ts,
        )

    plan = MigrationPlan(session_slug=session_slug, doc_path=doc_path)

    slugs = [t.slug for t in doc.threads]
    unslugged = [i for i, s in enumerate(slugs) if s is None]
    if unslugged:
        raise ValueError(
            f"Cannot migrate: thread(s) at position {unslugged} have no `=== slug`; "
            f"a slug is the thread's filename and its identity in `thrds.json` — "
            f"add one to each before migrating"
        )

    if doc.preamble:
        if PREAMBLE_SLUG in slugs:
            raise ValueError(
                f"Cannot migrate: doc has a preamble and a thread already named "
                f"{PREAMBLE_SLUG!r}; rename that thread before migrating"
            )
        preamble_thread = DocThread(
            messages=[DocMessage(content=doc.preamble)],
            slug=PREAMBLE_SLUG,
        )
        plan.threads.append(MigratedThread(
            index=PREAMBLE_INDEX,
            slug=PREAMBLE_SLUG,
            filename=thread_filename(PREAMBLE_INDEX, PREAMBLE_SLUG),
            text=serialize_thread(preamble_thread),
            entry=_entry_for(state, PREAMBLE_SLUG, default_target),
        ))

    for i, thread in enumerate(doc.threads, start=1):
        slug = thread.slug
        assert slug is not None  # guarded above
        plan.threads.append(MigratedThread(
            index=i,
            slug=slug,
            filename=thread_filename(i, slug),
            text=serialize_thread(thread),
            entry=_entry_for(state, slug, default_target),
        ))

    return plan


def apply_migration(session_dir: Path | str, state: SessionState, plan: MigrationPlan) -> list[Path]:
    """Write the planned thread files, drop the legacy doc, and update ``state``.

    Returns the paths written plus the removed doc, for the caller to stage in
    the session's git repo. ``state`` is mutated in place but **not** saved —
    the caller saves it alongside its own commit, so a failed write never
    leaves state claiming a migration that didn't land.
    """
    d = Path(session_dir)
    existing = thread_files(d)
    if existing:
        raise ValueError(
            f"Cannot migrate: {d} already has thread files "
            f"({', '.join(f.name for f in existing)}) — this session looks migrated"
        )

    touched: list[Path] = []
    for t in plan.threads:
        path = d / t.filename
        path.write_text(t.text)
        touched.append(path)

    doc = d / plan.doc_path
    if doc.exists():
        doc.unlink()
        touched.append(doc)

    state.session_slug = plan.session_slug
    state.threads = plan.threads_map
    state.doc_path = None
    state.staging_threads = {}
    state.prod_threads = {}
    state.prod_preamble_ts = {}
    state.staging_preamble_ts = None

    return touched
