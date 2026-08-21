"""Tests for `thrds.remotes` — the named-remotes resolution layer.

Verbs hold a `Remote` and stay role-agnostic; these pin the two facts the
verbs rely on: resolve order (the composite overlays remotes in sequence, so
prod-role must come last) and the partial-fetch content check.
"""
from __future__ import annotations

from thrds import SessionState, ThreadEntry, ThreadTarget, tracking
from thrds.remotes import Remote, has_threads, resolve


def _state(**threads: ThreadEntry) -> SessionState:
    return SessionState.new(
        session_slug='s', staging_channel='C0STAGE', threads=dict(threads),
    )


def test_resolve_order_is_staging_then_prod():
    """Load-bearing: the composite merges in this order, so a posted thread's
    prod copy overwrites its frozen staging copy — not the other way round."""
    assert list(resolve(_state())) == [tracking.STAGING, tracking.PROD]


def test_default_remotes_carry_their_roles_and_the_staging_channel():
    assert resolve(_state()) == {
        'staging': Remote(name='staging', role='staging', channel='C0STAGE'),
        'prod': Remote(name='prod', role='prod', channel=None),
    }


def test_remote_ref_and_label():
    rmt = resolve(_state())['staging']
    assert rmt.ref('slack') == 'refs/remotes/slack/staging'
    assert rmt.label('slack') == 'slack/staging'


def test_staging_has_threads_iff_any_draft_is_staged():
    empty = _state(alpha=ThreadEntry())
    staged = _state(alpha=ThreadEntry(staging_ts='1.1'))
    assert has_threads(resolve(empty)['staging'], empty) is False
    assert has_threads(resolve(staged)['staging'], staged) is True


def test_prod_has_threads_iff_any_thread_is_posted():
    drafted = _state(alpha=ThreadEntry(staging_ts='1.1'))
    posted = _state(alpha=ThreadEntry(
        state='posted', posted_ts='9.9', target=ThreadTarget(channel='C0PROD'),
    ))
    assert has_threads(resolve(drafted)['prod'], drafted) is False
    assert has_threads(resolve(posted)['prod'], posted) is True
