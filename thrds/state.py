"""Local session state for a thrds session — the write-through cache of thread ownership.

Written to ``thrds.json`` at the session root, tracked and gist-mirrored via
the git repo so multi-machine sees the same authoritative slug → thread_ts map
without needing to scan Slack. The Slack ``metadata`` field on each posted message
carries the same info (session_id, slug, kind) as belt-and-suspenders: if the
local state is lost or corrupted, ``thrds slack recover`` scans Slack filtered by
session_id and rebuilds this file.

Per-thread model (``specs/per-thread-model.md``)
------------------------------------------------
::

    private ephemeral channel (PEC)  ↔  gist  ↔  session  ↔  N threads
                                                  thread  ↔  one `NN-slug.md`

``threads`` maps each thread's slug to a :class:`ThreadEntry`: where its draft
lives in the staging channel, where it's destined (:class:`ThreadTarget`), and
its lifecycle state. **Destination is a property of the thread, not the
session** — that's what collapses the two use cases into one shape:

- batch: N threads, all targeting one channel with no ``thread_ts`` (the
  session-level ``prod_channel`` supplies it as a default, so no per-thread
  config is needed at all);
- reply: N threads, each targeting a different channel and/or an existing
  thread within it.

Legacy layout (pre-per-thread)
------------------------------
Sessions created before the per-thread model have a single ``doc_path`` holding
many ``===``-separated threads, with pointers split across ``staging_threads``
and ``prod_threads[channel][slug]``, and ``prod_channel`` as the *authority*
for prod pushes rather than a default. :attr:`SessionState.is_legacy` detects
these; ``thrds slack migrate`` converts one. The legacy fields are retained
(and still drive the pre-migration code paths) until every live session has
been migrated.

``platform`` identifies which subcommand group owns this session:

- ``"slack"``: the default; ``thrds slack …`` verbs operate on it.
- ``"discord"`` / ``"bsky"``: reserved for future subcommand groups.
- ``"capture"``: no platform target — gist-mirrored trajectory only, `thrds capture …`.

Old state files (pre-0.6) predate the field and default to ``"slack"`` on load.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


STATE_PATH = Path('thrds.json')  # flat filename (not `.thrds/state.json`) — GitHub gists reject directories
DEFAULT_GIST_REMOTE = 'g'  # matches ghpr's convention
CHANNEL_PREFIX_ENV = 'THRDS_CHANNEL_PREFIX'

VALID_PLATFORMS = ('slack', 'discord', 'bsky', 'capture')
DEFAULT_PLATFORM = 'slack'

# A thread's lifecycle. `draft` → not ready; `ready` → armed for push (set by
# hand or by a 🟢 reaction); `posted` → landed at its target; `dropped` →
# abandoned without posting. Archiving the staging channel is gated on every
# thread being `posted` or `dropped`.
VALID_THREAD_STATES = ('draft', 'ready', 'posted', 'dropped')
DEFAULT_THREAD_STATE = 'draft'
TERMINAL_THREAD_STATES = ('posted', 'dropped')


@dataclass
class ThreadTarget:
    """Where a thread is destined: a channel, and optionally a thread within it.

    ``thread_ts is None`` means "post as a new top-level message in
    ``channel``" — the batch case (six sibling messages into one channel).
    ``thread_ts`` set means "post as a reply into that existing thread" — the
    case of drafting a considered response to someone else's message.

    Destination is a property of the *thread*, not the session: that's what
    collapses both use cases into one shape. The session-level
    ``prod_channel`` is only a default for newly-added threads.
    """
    channel: str
    thread_ts: str | None = None

    def __post_init__(self) -> None:
        if not self.channel:
            raise ValueError("ThreadTarget.channel must be non-empty")


@dataclass
class ThreadEntry:
    """Per-thread state: where its draft lives, where it's going, and its status.

    ``staging_ts`` is the OP ts of this thread's draft in the staging channel.
    ``target`` is the prod destination (None until set). ``posted_ts`` is the
    ts of *our* message once it lands at the target — distinct from
    ``target.thread_ts``, which is the message we're replying *to*.
    """
    staging_ts: str | None = None
    target: ThreadTarget | None = None
    state: str = DEFAULT_THREAD_STATE
    posted_ts: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.target, dict):
            self.target = ThreadTarget(**self.target)
        if self.state not in VALID_THREAD_STATES:
            raise ValueError(
                f"Invalid thread state {self.state!r}; must be one of {VALID_THREAD_STATES}"
            )

    @property
    def is_terminal(self) -> bool:
        """True once this thread has been posted or explicitly dropped."""
        return self.state in TERMINAL_THREAD_STATES


def resolve_channel_prefix(session_override: str | None) -> str:
    """Resolve the channel prefix for a private-channel name.

    Precedence: explicit ``session_override`` > ``THRDS_CHANNEL_PREFIX`` env
    var > empty string. Env-scoped is the common case (user-namespaced private
    channels like ``rw-``); the session override is for one-off deviations from
    the user default without needing to unset the env var.
    """
    if session_override is not None:
        return session_override
    return os.environ.get(CHANNEL_PREFIX_ENV, '')


@dataclass
class SessionState:
    """Session-wide state persisted between `thrds` invocations.

    ``session_id`` is generated once at init and stamped into every posted
    message's Slack metadata; it's the key that lets ``recover`` find our
    threads via Slack alone.

    ``doc_path`` is the .md filename (relative to the session root) that this
    session is drafting — resolved at init and pinned; changing which doc a
    session drafts is a new session.

    ``channel_prefix`` overrides the ``THRDS_CHANNEL_PREFIX`` env var at
    session scope — usually left ``None`` (env-scoped is the common case for
    user-namespaced private channels).

    ``platform`` is set implicitly by the ``init`` subcommand used
    (``thrds slack init`` → ``"slack"``, ``thrds capture init`` → ``"capture"``,
    etc.). All subsequent verbs for that session must match; the ``slack_cli`` /
    ``capture_cli`` groups guard on this to fail fast with a clear error rather
    than blowing up deep inside a mismatched code path.
    """
    session_id: str
    doc_path: str | None = None
    session_slug: str | None = None
    prod_channel: str | None = None
    gist_id: str | None = None
    gist_remote: str = DEFAULT_GIST_REMOTE
    channel_prefix: str | None = None
    platform: str = DEFAULT_PLATFORM
    staging_channel: str | None = None
    staging_preamble_ts: str | None = None
    staging_threads: dict[str, str] = field(default_factory=dict)                    # slug → thread_ts
    staging_archived: bool = False                                                   # True once the PC has been archived (idempotence marker)
    prod_threads: dict[str, dict[str, str]] = field(default_factory=dict)            # channel → (slug → thread_ts)
    prod_preamble_ts: dict[str, str] = field(default_factory=dict)                   # channel → preamble ts
    workspace_emoji: dict[str, str] = field(default_factory=dict)                    # custom emoji name → local filename (relative to session dir)
    threads: dict[str, ThreadEntry] = field(default_factory=dict)                    # slug → per-thread state (per-thread model; see `threads_legacy`)

    def __post_init__(self) -> None:
        if self.platform not in VALID_PLATFORMS:
            raise ValueError(
                f"Invalid platform {self.platform!r}; must be one of {VALID_PLATFORMS}"
            )
        # `load` reconstructs via `cls(**json)`, so nested entries arrive as
        # plain dicts; coerce here so both load and direct construction yield
        # the same types.
        self.threads = {
            slug: entry if isinstance(entry, ThreadEntry) else ThreadEntry(**entry)
            for slug, entry in self.threads.items()
        }

    @classmethod
    def new(cls, **overrides: Any) -> 'SessionState':
        """Create a new SessionState with a fresh session_id (UUID4)."""
        return cls(session_id=str(uuid.uuid4()), **overrides)

    @classmethod
    def load(cls, root: Path | str = '.') -> 'SessionState':
        """Load from ``<root>/thrds.json``; raise if missing."""
        path = Path(root) / STATE_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"No thrds session state at {path}; run `thrds <platform> init` first."
            )
        return cls(**json.loads(path.read_text()))

    def save(self, root: Path | str = '.') -> None:
        """Persist to ``<root>/thrds.json``, creating the parent dir if needed."""
        path = Path(root) / STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + '\n')

    def get_thread_ts(self, channel: str, slug: str) -> str | None:
        """Look up the recorded thread_ts for ``(channel, slug)``; None if not present."""
        if channel == self.staging_channel:
            return self.staging_threads.get(slug)
        return self.prod_threads.get(channel, {}).get(slug)

    def set_thread_ts(self, channel: str, slug: str, thread_ts: str) -> None:
        """Record that ``slug`` was posted at ``thread_ts`` in ``channel``.

        Routes to ``staging_threads`` when ``channel == staging_channel``, else
        into ``prod_threads[channel]`` (creating the inner dict on first write).
        """
        if channel == self.staging_channel:
            self.staging_threads[slug] = thread_ts
        else:
            self.prod_threads.setdefault(channel, {})[slug] = thread_ts

    def threads_in(self, channel: str) -> dict[str, str]:
        """Return a snapshot dict of slug → thread_ts for ``channel`` (empty if none)."""
        if channel == self.staging_channel:
            return dict(self.staging_threads)
        return dict(self.prod_threads.get(channel, {}))

    # --- per-thread model (`threads`) ---------------------------------------

    @property
    def is_legacy(self) -> bool:
        """True for a session still on the one-doc-many-threads layout.

        Detected as "has legacy thread pointers but no ``threads`` map".
        Verbs on the per-thread model check this and direct the user to
        ``thrds slack migrate`` rather than silently operating on a session
        whose threads they can't see.
        """
        if self.threads:
            return False
        return bool(self.staging_threads or self.prod_threads)

    def thread(self, slug: str) -> ThreadEntry:
        """The :class:`ThreadEntry` for ``slug``, creating an empty one if absent.

        Creating-on-access is right here because a thread's existence is
        determined by its file on disk; ``thrds.json`` only accumulates what
        we've learned about it (where its draft landed, where it's going).
        """
        if slug not in self.threads:
            self.threads[slug] = ThreadEntry()
        return self.threads[slug]

    def target_for(self, slug: str) -> ThreadTarget | None:
        """Resolved destination for ``slug``: its own target, else the session default.

        Falls back to ``prod_channel`` as a bare channel target, which is what
        makes the batch case (N threads, all the same channel) require no
        per-thread configuration at all.
        """
        entry = self.threads.get(slug)
        if entry is not None and entry.target is not None:
            return entry.target
        if self.prod_channel is not None:
            return ThreadTarget(channel=self.prod_channel)
        return None

    def pending_threads(self) -> list[str]:
        """Slugs not yet in a terminal (``posted``/``dropped``) state, in slug order.

        Archiving the staging channel is gated on this being empty — other
        drafts being live is exactly why prod push must not auto-archive.
        """
        return sorted(slug for slug, e in self.threads.items() if not e.is_terminal)

    @property
    def doc_slug(self) -> str:
        """The session's slug: the local ID and the un-prefixed staging channel name.

        Prefers the explicit ``session_slug``; falls back to the ``doc_path``
        basename for legacy sessions, where the single doc *was* the session's
        identity. Migration pins ``session_slug`` before retiring ``doc_path``,
        so the staging channel name and session dir stay stable across it.
        """
        if self.session_slug is not None:
            return self.session_slug
        if self.doc_path is None:
            raise ValueError("neither session_slug nor doc_path is set on this session")
        return Path(self.doc_path).stem

    def staging_channel_name(self) -> str:
        """The Slack-side name to use when creating this session's staging PC.

        ``<prefix><doc_slug>``, where ``<prefix>`` comes from
        :func:`resolve_channel_prefix`. This is the name proposed to
        ``conversations.create``; the API may lowercase / replace invalid
        chars, so the returned channel object is the source of truth for the
        actual name Slack assigned.
        """
        return f"{resolve_channel_prefix(self.channel_prefix)}{self.doc_slug}"
