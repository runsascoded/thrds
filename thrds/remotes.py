"""Named remotes: the resolution layer between verbs and pseudo-remote roles.

`specs/remotes-model.md`: a remote is anywhere content can be fetched from or
pushed to — a Slack channel, a Discord server, a GH item — and `staging`/`prod`
are the *default topology*, not the model. This module is where a verb turns a
remote's name into what it needs: the tracking ref to gate against, and how to
observe the remote's current content. Verbs hold a :class:`Remote` and stay
role-agnostic; only :func:`resolve` and :func:`observe` know what a role means.

:func:`resolve` derives the two default remotes from existing session fields
and overlays the session's ``remotes:`` config section (`thrds.yml`) — extra
remotes, per-remote channels, and per-remote chrome. Chrome defaults are
presets keyed by role, so config records only deviations (Ryan: a remote
called "staging" gets appropriate chrome for its platform; prod none; a gthb
item remote would default to a gist-link footer when that platform lands).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .md import serialize_thread
from .state import SessionState, StagingChrome
from .threadfile import thread_files
from . import tracking


@dataclass(frozen=True)
class Remote:
    """One fetch/push-able place, named like a git remote.

    ``name`` is the ref component (``refs/remotes/<name>``) and the display
    label; ``role`` selects the behavior bundle (how to observe it, whether
    writes converge or append). They coincide for the two defaults, and
    diverge once a session declares, say, a second prod-role remote under
    its own name. ``chrome`` is what extra affordances messages at this
    remote carry (None = none — the prod default).
    """
    name: str
    role: str
    channel: str | None = None
    chrome: StagingChrome | None = field(default=None, compare=True)

    @property
    def ref(self) -> str:
        return tracking.ref_name(self.name)

    @property
    def base_ref(self) -> str:
        """What HEAD has incorporated from this remote — the push gate's
        operand, distinct from `ref`'s "what we last saw"."""
        return tracking.base_ref_name(self.name)


ROLES = (tracking.STAGING, tracking.PROD)
CHROME_PRESETS = ('footer', 'none')
_ENTRY_KEYS = {'role', 'channel', 'chrome'}
_NAME_RE = re.compile(r'[a-z0-9][a-z0-9_-]*')


def _chrome(spec, name: str, role: str, state: SessionState) -> StagingChrome | None:
    """A remote's chrome: explicit config > session default > role preset.

    ``spec`` (the entry's ``chrome:`` value) is a preset name (``footer`` /
    ``none``) or a mapping of :class:`StagingChrome` fields. Absent, the
    default staging remote keeps honoring the session-level
    ``staging_chrome`` knob; anything else falls to its role's preset —
    staging-role renders the footer, prod-role renders nothing.
    """
    if spec is None:
        if name == tracking.STAGING:
            return state.staging_chrome
        return StagingChrome() if role == tracking.STAGING else None
    if isinstance(spec, str):
        if spec == 'none':
            return None
        if spec == 'footer':
            return StagingChrome()
        raise ValueError(
            f"unknown chrome preset {spec!r} for remote {name!r}; "
            f"presets: {', '.join(CHROME_PRESETS)}"
        )
    if isinstance(spec, dict):
        try:
            return StagingChrome(**spec)
        except TypeError as e:
            raise ValueError(f'invalid chrome config for remote {name!r}: {e}')
    raise ValueError(
        f'chrome for remote {name!r} must be a preset name or a mapping, '
        f'got {type(spec).__name__}'
    )


def _check_entry(name: str, entry) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f'remote {name!r} must be a mapping, got {type(entry).__name__}')
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise ValueError(
            f"unknown key(s) for remote {name!r}: {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(_ENTRY_KEYS))}"
        )
    return entry


def resolve(state: SessionState) -> dict[str, Remote]:
    """This session's remotes, in composite-merge order.

    The default topology (``staging`` from the session's staging channel,
    ``prod`` as the per-thread-targets role) overlaid with the ``remotes:``
    config section: the two defaults accept ``channel``/``chrome`` overrides
    (their roles are fixed), and extra entries declare new remotes — a
    staging-role entry must name its channel (its pointers inherit it), a
    prod-role entry may (per-thread pointers carry their own).

    Order is load-bearing: the composite overlays remotes in sequence, so
    every staging-role remote comes before every prod-role one — "prod is
    canonical for a posted thread", stated once. Within a role, defaults
    first, then declaration order. Raises ``ValueError`` on invalid config;
    `_load_state` surfaces that as a usage error once per invocation.
    """
    declared = {n: _check_entry(n, e) for n, e in (state.remotes or {}).items()}
    out: list[Remote] = []
    for name, channel in (
        (tracking.STAGING, state.staging_channel), (tracking.PROD, None),
    ):
        entry = declared.get(name, {})
        if entry.get('role', name) != name:
            raise ValueError(
                f"remote {name!r} is a default; its role is fixed "
                f"(got {entry['role']!r})"
            )
        out.append(Remote(
            name=name, role=name,
            channel=entry.get('channel', channel),
            chrome=_chrome(entry.get('chrome'), name, name, state),
        ))
    for name, entry in declared.items():
        if name in (tracking.STAGING, tracking.PROD):
            continue
        if name == tracking.COMPOSITE:
            raise ValueError(
                f"{tracking.COMPOSITE!r} is the derived merge base, not a "
                f"declarable remote"
            )
        if not _NAME_RE.fullmatch(name):
            raise ValueError(
                f'invalid remote name {name!r}: must match {_NAME_RE.pattern}'
            )
        role = entry.get('role')
        if role not in ROLES:
            raise ValueError(
                f"remote {name!r} needs a role in {ROLES} (got {role!r})"
            )
        if role == tracking.STAGING and not entry.get('channel'):
            raise ValueError(
                f'staging-role remote {name!r} needs a channel — its thread '
                f'pointers inherit it'
            )
        out.append(Remote(
            name=name, role=role,
            channel=entry.get('channel'),
            chrome=_chrome(entry.get('chrome'), name, role, state),
        ))
    out.sort(key=lambda r: 0 if r.role == tracking.STAGING else 1)
    return {r.name: r for r in out}


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
        return any(
            (p := e.remotes.get(remote.name)) is not None and p.ts is not None
            for e in state.threads.values()
        )
    return any(e.upstream == remote.name for e in state.threads.values())
