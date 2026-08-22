"""Tests for `thrds.remotes` — the named-remotes resolution layer.

Verbs hold a `Remote` and stay role-agnostic; these pin the facts the verbs
rely on: resolve order (the composite overlays remotes in sequence, so
prod-role must come last), the `remotes:` config overlay with its validation,
and chrome resolution (explicit config > session default > role preset).
"""
from __future__ import annotations

import pytest

from thrds import SessionState, ThreadEntry, ThreadTarget, tracking
from thrds.remotes import Remote, has_threads, resolve
from thrds.state import RemotePointer, StagingChrome


def _state(remotes: dict | None = None, **threads: ThreadEntry) -> SessionState:
    return SessionState.new(
        session_slug='s', staging_channel='C0STAGE',
        remotes=remotes or {}, threads=dict(threads),
    )


def test_resolve_order_is_staging_then_prod():
    """Load-bearing: the composite merges in this order, so a posted thread's
    prod copy overwrites its frozen staging copy — not the other way round."""
    assert list(resolve(_state())) == [tracking.STAGING, tracking.PROD]


def test_default_remotes_carry_roles_channel_and_preset_chrome():
    assert resolve(_state()) == {
        'staging': Remote(
            name='staging', role='staging', channel='C0STAGE',
            chrome=StagingChrome(),
        ),
        'prod': Remote(name='prod', role='prod', channel=None, chrome=None),
    }


def test_remote_ref_is_name_first_like_git():
    """Converged with ghpr: git's own shape is `refs/remotes/<remote>/…`, so
    the remote's name is the first component and `git branch -r` reads as the
    remote list."""
    rmts = resolve(_state())
    assert [r.ref for r in rmts.values()] == [
        'refs/remotes/staging', 'refs/remotes/prod',
    ]


# --- the `remotes:` config section ---


def test_extra_remotes_slot_into_role_order():
    """All staging-role remotes precede all prod-role ones in the composite
    merge; within a role, defaults first, then declaration order."""
    rmts = resolve(_state(remotes={
        'archive': {'role': 'prod'},
        'scratch': {'role': 'staging', 'channel': 'C0SCRATCH'},
    }))
    assert list(rmts) == ['staging', 'scratch', 'prod', 'archive']
    assert rmts['scratch'] == Remote(
        name='scratch', role='staging', channel='C0SCRATCH',
        chrome=StagingChrome(),
    )
    assert rmts['archive'] == Remote(name='archive', role='prod')


def test_defaults_accept_channel_and_chrome_overrides():
    rmts = resolve(_state(remotes={
        'staging': {'chrome': 'none'},
        'prod': {'channel': 'C0FIXED'},
    }))
    assert rmts['staging'].chrome is None
    assert rmts['prod'].channel == 'C0FIXED'


def test_chrome_mapping_overrides_fields():
    rmts = resolve(_state(remotes={
        'staging': {'chrome': {'gist_link': False, 'target_link': True}},
    }))
    assert rmts['staging'].chrome == StagingChrome(gist_link=False, target_link=True)


def test_session_staging_chrome_still_feeds_the_default_staging_remote():
    """The session-level knob keeps working when config doesn't override it."""
    state = _state()
    state.staging_chrome = StagingChrome(gist_link=False)
    assert resolve(state)['staging'].chrome == StagingChrome(gist_link=False)


def test_default_roles_are_fixed():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'staging': {'role': 'prod'}}))
    assert str(e.value) == (
        "remote 'staging' is a default; its role is fixed (got 'prod')"
    )


def test_unknown_entry_key_is_refused():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'scratch': {'role': 'staging', 'chanel': 'C1'}}))
    assert str(e.value) == (
        "unknown key(s) for remote 'scratch': chanel; "
        'allowed: channel, chrome, role'
    )


def test_upstream_is_not_a_declarable_remote():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'upstream': {'role': 'prod'}}))
    assert str(e.value) == (
        "'upstream' is the derived merge base, not a declarable remote"
    )


def test_extra_staging_role_remote_requires_a_channel():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'scratch': {'role': 'staging'}}))
    assert str(e.value) == (
        "staging-role remote 'scratch' needs a channel — its thread pointers "
        'inherit it'
    )


def test_unknown_chrome_preset_is_refused():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'staging': {'chrome': 'fancy'}}))
    assert str(e.value) == (
        "unknown chrome preset 'fancy' for remote 'staging'; presets: footer, none"
    )


def test_missing_role_on_an_extra_remote_is_refused():
    with pytest.raises(ValueError) as e:
        resolve(_state(remotes={'scratch': {'channel': 'C1'}}))
    assert str(e.value) == (
        "remote 'scratch' needs a role in ('staging', 'prod') (got None)"
    )


# --- has_threads (the partial-fetch / composite guard) ---


def test_staging_has_threads_iff_any_pointer_at_this_remotes_name():
    empty = _state(alpha=ThreadEntry())
    staged = _state(alpha=ThreadEntry(staging_ts='1.1'))
    assert has_threads(resolve(empty)['staging'], empty) is False
    assert has_threads(resolve(staged)['staging'], staged) is True
    # A pointer at a *different* staging-role remote doesn't count.
    scratch_cfg = {'scratch': {'role': 'staging', 'channel': 'C0SCRATCH'}}
    other = _state(
        remotes=scratch_cfg,
        alpha=ThreadEntry(remotes={'scratch': RemotePointer(ts='5.5')}),
    )
    assert has_threads(resolve(other)['staging'], other) is False
    assert has_threads(resolve(other)['scratch'], other) is True


def test_prod_has_threads_iff_any_thread_upstreams_to_it():
    drafted = _state(alpha=ThreadEntry(staging_ts='1.1'))
    posted = _state(alpha=ThreadEntry(
        state='posted', posted_ts='9.9', target=ThreadTarget(channel='C0PROD'),
    ))
    assert has_threads(resolve(drafted)['prod'], drafted) is False
    assert has_threads(resolve(posted)['prod'], posted) is True
