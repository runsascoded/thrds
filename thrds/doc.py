"""Multi-thread docs: sibling top-level messages in one channel, each with its own replies.

A `Doc` is the unit of authoring (one `.md` file, one Slack channel, one PC) —
several sibling `DocThread`s posted into a channel, optionally preceded by a
`preamble` (a bare top-level message with no replies) and optionally cross-linking
each other by `slug`. Compare `Thread`, which models a single OP + replies; a
`Doc` is a channel-level container of Threads.

Ownership
---------
Every `DocMessage` carries an optional `author` — ``None`` means the message belongs
to the session owner (the token's user) and is editable/deletable by `sync_doc`; a
non-``None`` author is a foreign message (a colleague replied in your thread) captured
on pull for faithful round-trip but never touched by sync. Analogous to
`Message.editable=False` in core sync — foreign messages are preserved in place,
never edited or deleted, never counted against desired slots.

Top-level threads in a `Doc` are always ours (started by us). Replying to a thread
someone else started is a future case; `DocThread.parent_ts` is the design hook for
it (unused in v0.1 — leave `None`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import SyncResult


@dataclass
class DocMessage:
    """One message in a `DocThread` (OP or reply).

    ``author=None`` — belongs to us, syncable. A username (str) marks a
    foreign message (someone else's reply in our thread); preserved by
    sync, never edited.
    """
    content: str
    author: str | None = None


@dataclass
class DocThread:
    """One top-level message + its replies within a `Doc`.

    ``messages[0]`` is the OP; ``messages[1:]`` are replies in order. ``slug``
    names the thread for cross-`Doc` references — a ``[text](#slug)`` link in
    another thread's body resolves to this thread's permalink at phase-4
    render time. Threads without a slug can still be posted, they just can't
    be linked to.

    ``parent_ts`` is a design hook for the "reply to a thread someone else
    started" case: when set (future v0.2+), ``messages`` are replies going
    into an existing thread at ``parent_ts`` and no OP is created. Unused
    in v0.1 — leave ``None``.
    """
    messages: list[DocMessage]
    slug: str | None = None
    parent_ts: str | None = None


@dataclass
class Doc:
    """Several sibling threads posted into one channel, optionally cross-linked."""
    threads: list[DocThread] = field(default_factory=list)
    preamble: str | None = None


@dataclass
class DocSyncResult:
    """Outcome of syncing a `Doc` into a Slack channel (staging or prod).

    ``preamble_ts`` is the message ts of the preamble, or None if the doc had
    no preamble. ``thread_ts_by_slug`` maps each slugged thread's slug → its
    OP ts. ``thread_results`` holds the per-thread `SyncResult` from `sync()`
    for callers that need the fine-grained action log. ``deleted_slugs`` are
    slugs that were previously recorded in state but absent from the current
    doc; their threads were deleted (staging terraform) or preserved (prod
    additive) depending on the sync mode.
    """
    channel: str
    preamble_ts: str | None
    thread_ts_by_slug: dict[str, str]
    thread_results: dict[str, 'SyncResult']
    deleted_slugs: list[str] = field(default_factory=list)


@dataclass
class Frontmatter:
    """YAML frontmatter parsed from a `.md` doc.

    All fields are optional at the doc level — session state
    (`thrds.json`) supplies defaults; frontmatter overrides them per-doc.
    """
    channel: str | None = None
    thread_ts: str | None = None
    session_id: str | None = None
