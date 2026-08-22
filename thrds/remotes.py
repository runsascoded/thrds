"""Named remotes: the resolution layer between verbs and pseudo-remote roles.

`specs/remotes-model.md`: a remote is anywhere content can be fetched from or
pushed to — a Slack channel, a Discord server, a GH item — and `staging`/`prod`
are the *default topology*, not the model. This module is where a verb turns a
remote's name into what it needs: the tracking ref to gate against, and how to
observe the remote's current content. Verbs hold a :class:`Remote` and stay
role-agnostic; only :func:`resolve` and :func:`observe` know what a role means.

Today :func:`resolve` derives the two default remotes from existing session
fields. The `remotes` config section the spec calls for waits on per-remote
thread pointers (a second staging-role remote needs its own slug → ts map, and
channel-parameterized pulls) — declaring remotes the readers can't honor would
be config that lies.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .md import serialize_thread
from .state import SessionState
from .threadfile import thread_files
from . import tracking


@dataclass(frozen=True)
class Remote:
    """One fetch/push-able place, named like a git remote.

    ``name`` is the ref component (``refs/remotes/<name>``) and the display
    label; ``role`` selects the behavior bundle (how to observe it, whether
    writes converge or append). They coincide for the two defaults, and
    diverge once a session can declare, say, a second prod-role remote under
    its own name.
    """
    name: str
    role: str
    channel: str | None = None

    @property
    def ref(self) -> str:
        return tracking.ref_name(self.name)


def resolve(state: SessionState) -> dict[str, Remote]:
    """This session's remotes, in composite-merge order.

    Order is load-bearing: the composite overlays remotes in sequence, so a
    thread present in more than one must be listed draft-source first and
    canonical-source last. Prod after staging is the existing "prod is
    canonical for a posted thread" rule, stated once.
    """
    return {
        tracking.STAGING: Remote(
            name=tracking.STAGING, role=tracking.STAGING, channel=state.staging_channel,
        ),
        tracking.PROD: Remote(name=tracking.PROD, role=tracking.PROD),
    }


def observe(
    remote: Remote,
    client,
    state: SessionState,
    session_dir: Path,
    slugs: Iterable[str] | None = None,
) -> dict[str, str]:
    """The remote's current content, as ``{filename: text}`` `pull` would write.

    A thread whose OP was deleted comes back with no messages; it's *absent*
    rather than present-and-empty, so a fetch records the file going away
    instead of a lone blank line. ``slugs`` scopes the read to those threads.

    An observation touches no files: custom emoji are substituted by their
    deterministic filenames without being downloaded (``download_emoji=False``),
    so the rendered text still matches what `pull` would write.
    """
    names = {tf.slug: tf.name for tf in thread_files(session_dir)}
    return {
        names.get(t.slug, f'{t.slug}.md'): serialize_thread(t)
        for t in client.pull_thread_states(
            remote, state, session_dir=session_dir, slugs=slugs, download_emoji=False,
        )
        if t.messages
    }


def has_threads(remote: Remote, state: SessionState) -> bool:
    """Whether this remote should currently hold any of the session's threads.

    The partial-fetch guard: a remote that has threads but no tracking ref
    can't be skipped — the composite would misrecord its threads as deleted.
    """
    if remote.role == tracking.STAGING:
        return any(e.staging_ts is not None for e in state.threads.values())
    return any(e.state == 'posted' for e in state.threads.values())
