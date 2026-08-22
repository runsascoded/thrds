"""Tests for the writer side of named remotes: `sync_threads_staging(remote=…)`.

The reader tests (`test_pull_thread_states`) pin where a remote is *read*;
these pin where it's *written*: posts go to the remote's channel, and the
thread's pointer lands at the remote's name — the default staging pointer is
never touched by a push to a different staging-role remote.
"""
from __future__ import annotations

import pytest

from thrds import DocMessage, DocThread, SessionState, ThreadEntry
from thrds.remotes import resolve
from thrds.slack import SlackClient
from thrds.state import RemotePointer


def _thread(slug: str, *contents: str) -> DocThread:
    return DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)


@pytest.fixture
def client(monkeypatch):
    c = SlackClient(token='xoxp-fake', channel='C0INIT')
    c.calls = []

    def fake_request(endpoint, data=None, **kw):
        c.calls.append({'endpoint': endpoint, **(data or {})})
        if endpoint == 'conversations.replies':
            return {'messages': []}
        return {'ok': True, 'ts': '7.7'}

    monkeypatch.setattr(c, '_request', fake_request)
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))
    return c


def _state(tmp_path, monkeypatch, **kw) -> SessionState:
    monkeypatch.chdir(tmp_path)
    return SessionState.new(session_slug='s', staging_channel='C0STAGE', **kw)


def test_sync_posts_to_the_remotes_channel_and_pointer(client, tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, remotes={
        'scratch': {'role': 'staging', 'channel': 'C0SCRATCH'},
    })
    client.sync_threads_staging(
        [_thread('a', 'Body.')], state, pace=0.0,
        remote=resolve(state)['scratch'],
    )
    posts = [c for c in client.calls if c['endpoint'] == 'chat.postMessage']
    assert [p['channel'] for p in posts] == ['C0SCRATCH']
    assert state.threads['a'].remotes == {'scratch': RemotePointer(ts='7.7')}


def test_default_push_still_writes_the_staging_pointer(client, tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    client.sync_threads_staging([_thread('a', 'Body.')], state, pace=0.0)
    posts = [c for c in client.calls if c['endpoint'] == 'chat.postMessage']
    assert [p['channel'] for p in posts] == ['C0STAGE']
    assert state.threads['a'].remotes == {'staging': RemotePointer(ts='7.7')}


def test_terraform_scope_is_per_remote(client, tmp_path, monkeypatch):
    """A thread staged at the *default* remote but absent from this push must
    not be terraformed away: staleness is judged by pointers at the pushed
    remote, not by any staging pointer anywhere."""
    state = _state(tmp_path, monkeypatch, remotes={
        'scratch': {'role': 'staging', 'channel': 'C0SCRATCH'},
    })
    state.threads['b'] = ThreadEntry(staging_ts='1.1')  # staged at default only
    client.sync_threads_staging(
        [_thread('a', 'Body.')], state, pace=0.0,
        remote=resolve(state)['scratch'],
    )
    assert 'b' in state.threads
    assert [c for c in client.calls if c['endpoint'] == 'chat.delete'] == []
