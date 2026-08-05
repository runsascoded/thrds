"""Local session state for a thrds session — the write-through cache of thread ownership.

Written to ``.thrds/state.json`` at the session root, tracked and gist-mirrored via
the git repo so multi-machine sees the same authoritative slug → thread_ts map
without needing to scan Slack. The Slack ``metadata`` field on each posted message
carries the same info (session_id, slug, kind) as belt-and-suspenders: if the
local state is lost or corrupted, ``thrds recover`` scans Slack filtered by
session_id and rebuilds this file.

Session : .md doc : staging PC is 1 : 1 : 1 — a single doc per session, tracked
by `doc_path` (relative to the session root). Two channel scopes on a session:

- ``staging_channel`` + ``staging_threads``: the single-member private channel used
  for preview/iteration. Terraformed on each staging push.
- ``prod_threads[channel][slug]``: threads created in real target channels. Additive;
  subsequent prod pushes sync in place, never terraforming.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


STATE_PATH = Path('.thrds/state.json')
DEFAULT_GIST_REMOTE = 'g'  # matches ghpr's convention
CHANNEL_PREFIX_ENV = 'THRDS_CHANNEL_PREFIX'


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
    """
    session_id: str
    doc_path: str | None = None
    prod_channel: str | None = None
    gist_id: str | None = None
    gist_remote: str = DEFAULT_GIST_REMOTE
    channel_prefix: str | None = None
    staging_channel: str | None = None
    staging_preamble_ts: str | None = None
    staging_threads: dict[str, str] = field(default_factory=dict)                    # slug → thread_ts
    prod_threads: dict[str, dict[str, str]] = field(default_factory=dict)            # channel → (slug → thread_ts)
    prod_preamble_ts: dict[str, str] = field(default_factory=dict)                   # channel → preamble ts

    @classmethod
    def new(cls, **overrides: Any) -> 'SessionState':
        """Create a new SessionState with a fresh session_id (UUID4)."""
        return cls(session_id=str(uuid.uuid4()), **overrides)

    @classmethod
    def load(cls, root: Path | str = '.') -> 'SessionState':
        """Load from ``<root>/.thrds/state.json``; raise if missing."""
        path = Path(root) / STATE_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"No thrds session state at {path}; run `thrds init` first."
            )
        return cls(**json.loads(path.read_text()))

    def save(self, root: Path | str = '.') -> None:
        """Persist to ``<root>/.thrds/state.json``, creating the parent dir if needed."""
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

    @property
    def doc_slug(self) -> str:
        """The doc's slug, derived from ``doc_path`` basename (minus .md extension).

        Used both as the local ID for the doc and as the un-prefixed portion
        of the staging channel name. Raises if ``doc_path`` is unset.
        """
        if self.doc_path is None:
            raise ValueError("doc_path is not set on this session")
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
