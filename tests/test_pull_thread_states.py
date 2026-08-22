"""Tests for `SlackClient.pull_thread_states` — the remote-parameterized reader.

The CLI suites fake this method, so the real one is pinned here against a
scripted transport. What matters is exactly what the parameterization bought:
which channel a role reads, which pointers select the threads, and the
upstream gate that keeps a reopened thread's frozen prod copy from being
pulled over the revision in progress.
"""
from __future__ import annotations

import pytest

from thrds import DocMessage, DocThread, SessionState, ThreadEntry
from thrds.remotes import Remote
from thrds.slack import SlackClient
from thrds.state import RemotePointer


class _FakeSlackClient(SlackClient):
    """SlackClient with `_request` stubbed: replies keyed by (channel, ts)."""

    def __init__(self, replies: dict[tuple[str, str], list[dict]]):
        super().__init__(token='x', channel='C_INIT')
        self._replies = replies
        self.reply_calls: list[tuple[str, str]] = []

    def _request(self, endpoint, data=None, method='POST'):
        if endpoint == 'auth.test':
            return {'ok': True, 'user_id': 'U_ME', 'bot_id': None}
        if endpoint == 'conversations.replies':
            key = (data['channel'], data['ts'])
            self.reply_calls.append(key)
            return {'ok': True, 'messages': self._replies.get(key, [])}
        raise NotImplementedError(f'no handler for {endpoint}')


def _msg(ts: str, text: str) -> dict:
    return {'ts': ts, 'text': text, 'user': 'U_ME'}


def _state(**threads: ThreadEntry) -> SessionState:
    return SessionState.new(session_slug='s', staging_channel='C0STAGE', threads=dict(threads))


STAGING = Remote(name='staging', role='staging', channel='C0STAGE')
PROD = Remote(name='prod', role='prod')


def test_staging_role_reads_the_remotes_channel_via_the_remotes_pointers():
    client = _FakeSlackClient({
        ('C0STAGE', '1.1'): [_msg('1.1', 'Alpha OP.')],
        ('C0STAGE', '2.2'): [_msg('2.2', 'Beta OP.')],
    })
    state = _state(
        alpha=ThreadEntry(staging_ts='1.1'),
        beta=ThreadEntry(staging_ts='2.2'),
        gamma=ThreadEntry(),  # no pointer at this remote → not read
    )
    threads = client.pull_thread_states(STAGING, state)
    assert threads == [
        DocThread(slug='alpha', messages=[DocMessage(content='Alpha OP.')]),
        DocThread(slug='beta', messages=[DocMessage(content='Beta OP.')]),
    ]
    assert sorted(client.reply_calls) == [('C0STAGE', '1.1'), ('C0STAGE', '2.2')]


def test_a_second_staging_role_remote_reads_its_own_channel_and_pointers():
    """The point of the parameterization: nothing about 'staging' is baked in.
    A thread's pointer at THIS remote's name selects it, and the remote's own
    channel is read — `state.staging_channel` is never consulted."""
    scratch = Remote(name='scratch', role='staging', channel='C0SCRATCH')
    client = _FakeSlackClient({
        ('C0SCRATCH', '5.5'): [_msg('5.5', 'Scratch draft.')],
    })
    state = _state(
        alpha=ThreadEntry(remotes={'scratch': RemotePointer(ts='5.5')}),
        beta=ThreadEntry(staging_ts='2.2'),  # pointer at 'staging', not 'scratch'
    )
    threads = client.pull_thread_states(scratch, state)
    assert threads == [
        DocThread(slug='alpha', messages=[DocMessage(content='Scratch draft.')]),
    ]
    assert client.reply_calls == [('C0SCRATCH', '5.5')]


def test_staging_role_without_a_channel_is_refused():
    client = _FakeSlackClient({})
    with pytest.raises(ValueError) as e:
        client.pull_thread_states(Remote(name='staging', role='staging'), _state())
    assert str(e.value) == (
        "No channel for remote 'staging' — the session hasn't pushed a "
        'staging Doc yet.'
    )


def _posted(state_name: str = 'posted') -> ThreadEntry:
    return ThreadEntry(
        state=state_name,
        remotes={
            'staging': RemotePointer(ts='1.1'),
            'prod': RemotePointer(ts='9.9', msg_ts=['9.9', '9.10'], channel='C0PROD'),
        },
    )


def test_prod_role_reads_only_our_own_messages_from_the_pointer_channel():
    client = _FakeSlackClient({
        ('C0PROD', '9.9'): [
            _msg('9.9', 'Posted OP.'),
            {'ts': '9.5', 'text': 'Foreign reply.', 'user': 'U_OTHER'},
            _msg('9.10', 'Posted reply.'),
        ],
    })
    threads = client.pull_thread_states(PROD, _state(alpha=_posted()))
    assert threads == [
        DocThread(slug='alpha', messages=[
            DocMessage(content='Posted OP.'),
            DocMessage(content='Posted reply.'),
        ]),
    ]
    assert client.reply_calls == [('C0PROD', '9.9')]


def test_prod_role_skips_a_thread_whose_upstream_moved_back_to_staging():
    """`reopen` keeps the prod pointers but flips state to draft — the thread
    is being revised in staging, so its frozen prod copy must not be pulled
    over the revision. The gate is `entry.upstream != remote.name`, stated in
    remotes vocabulary."""
    client = _FakeSlackClient({
        ('C0PROD', '9.9'): [_msg('9.9', 'Posted OP.')],
    })
    reopened = _posted(state_name='draft')
    assert reopened.remotes['prod'].ts == '9.9'  # pointers survive reopen
    threads = client.pull_thread_states(PROD, _state(alpha=reopened))
    assert threads == []
    assert client.reply_calls == []


def test_prod_role_channel_falls_back_to_the_remotes_channel():
    """A prod-role remote with its own channel (one target channel for the
    whole session) supplies it to pointers that don't record one."""
    fixed = Remote(name='prod', role='prod', channel='C0FIXED')
    client = _FakeSlackClient({
        ('C0FIXED', '9.9'): [_msg('9.9', 'Posted OP.')],
    })
    entry = ThreadEntry(
        state='posted',
        remotes={'prod': RemotePointer(ts='9.9', msg_ts=['9.9'])},
    )
    threads = client.pull_thread_states(fixed, _state(alpha=entry))
    assert threads == [
        DocThread(slug='alpha', messages=[DocMessage(content='Posted OP.')]),
    ]


def test_slugs_scope_both_roles():
    client = _FakeSlackClient({
        ('C0STAGE', '1.1'): [_msg('1.1', 'Alpha OP.')],
        ('C0STAGE', '2.2'): [_msg('2.2', 'Beta OP.')],
    })
    state = _state(
        alpha=ThreadEntry(staging_ts='1.1'), beta=ThreadEntry(staging_ts='2.2'),
    )
    threads = client.pull_thread_states(STAGING, state, slugs=['beta'])
    assert threads == [
        DocThread(slug='beta', messages=[DocMessage(content='Beta OP.')]),
    ]
