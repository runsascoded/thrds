"""Tests for the sender-change delete+repost cascade
(``specs/per-message-sender.md`` §Aggressive-mode extension).

Uses `MockClient`-style spies rather than a live SlackClient — the sync
layer is client-agnostic; SlackClient-specific tests (list_messages
sender-field population, get_reactions endpoint shape) live elsewhere.
"""
from __future__ import annotations

import pytest

from thrds import (
    ActionType,
    Message,
    Msg,
    SenderChangeForbidden,
    SenderChangePolicy,
    SyncOptions,
    Thread,
    sync,
)


class _Client:
    """Sender-aware mock client: records post/delete calls, supports
    reactions scripting via `reactions_by_id`."""
    def __init__(
        self,
        threads: dict[str, list[Message]] | None = None,
        *,
        username: str | None = None,
        icon_url: str | None = None,
        icon_emoji: str | None = None,
        reactions_by_id: dict[str, list] | None = None,
    ):
        self.threads: dict[str, list[Message]] = threads or {}
        self.username = username
        self.icon_url = icon_url
        self.icon_emoji = icon_emoji
        self._reactions = reactions_by_id or {}
        self.post_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.reactions_calls: list[str] = []
        self._next = 100

    def _new_id(self) -> str:
        self._next += 1
        return f"n{self._next}"

    def list_messages(self, thread_id):
        return list(self.threads.get(thread_id, []))

    def post(self, content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None):
        self.post_calls.append({
            'content': content, 'thread_id': thread_id,
            'username': username, 'icon_url': icon_url, 'icon_emoji': icon_emoji,
        })
        msg = Message(id=self._new_id(), content=content, sender_username=username, sender_icon_url=icon_url)
        self.threads.setdefault(thread_id or msg.id, []).append(msg)
        return msg

    def edit(self, message_id, content):
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    msgs[i] = Message(id=message_id, content=content,
                                       sender_username=m.sender_username,
                                       sender_icon_url=m.sender_icon_url)
                    return msgs[i]
        raise ValueError(f"no msg {message_id}")

    def delete(self, message_id):
        self.delete_calls.append(message_id)
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    msgs.pop(i)
                    return

    def get_reactions(self, message_id):
        self.reactions_calls.append(message_id)
        return list(self._reactions.get(message_id, []))


def _live(id_, content, *, sender_username='bot', sender_icon_url='https://old.png', editable=True):
    """Compact `Message` builder with sender fields defaulted to non-None
    so mismatch detection is actually enabled (`_sender_mismatch` bails
    if all live sender fields are None)."""
    return Message(id=id_, content=content, editable=editable,
                   sender_username=sender_username, sender_icon_url=sender_icon_url)


# --- strict-default: any mismatch raises ---

def test_sync_no_policy_raises_on_reply_sender_mismatch():
    """`sync(...)` without `sender_change` policy → `SenderChangeForbidden`."""
    client = _Client({'t1': [
        _live('op', 'OP text'),
        _live('r1', 'Reply text', sender_username='old-name'),
    ]})
    desired = Thread(messages=[
        'OP text',  # unchanged
        Msg(content='Reply text', username='new-name'),
    ])
    with pytest.raises(SenderChangeForbidden, match='no `SyncOptions.sender_change` policy'):
        sync(client, desired, thread_id='t1')


def test_sync_no_policy_no_mismatch_still_skips_cleanly():
    """Policy-less sync with content match + no sender diff → plain SKIP, no raise."""
    client = _Client(
        {'t1': [_live('op', 'OP', sender_username='bot', sender_icon_url='https://old.png')]},
        icon_url='https://old.png',   # client default matches live's stored icon
    )
    desired = Thread(messages=[Msg(content='OP', username='bot')])
    result = sync(client, desired, thread_id='t1')
    assert [a.type for a in result.actions] == [ActionType.SKIP]


# --- hard-forbid: OP sender change never allowed ---

def test_sync_op_sender_change_forbidden_even_with_policy():
    """OP (index 0) sender change → hard-forbid, even with permissive policy."""
    client = _Client({'t1': [_live('op', 'OP text', sender_username='old-op-name')]})
    desired = Thread(messages=[Msg(content='OP text', username='new-op-name')])
    policy = SenderChangePolicy(max_reposts=99, lose_reactions_ok=True)
    with pytest.raises(SenderChangeForbidden, match='OP.*always forbidden'):
        sync(client, desired, thread_id='t1', options=SyncOptions(sender_change=policy))


# --- hard-forbid: foreign msg after target ---

def test_sync_foreign_msg_after_target_forbidden():
    """Cascade cannot cross a foreign msg (would reorder past uneditable)."""
    client = _Client({'t1': [
        _live('op', 'OP', sender_username='bot'),
        _live('r1', 'ours 1', sender_username='old-name'),
        _live('f1', 'foreign reply', editable=False),
        _live('r2', 'ours 2', sender_username='bot'),
    ]})
    desired = Thread(messages=[
        'OP',
        Msg(content='ours 1', username='new-name'),
        # Foreign msg is filtered from `desired`; sync operates on ours-only.
        'ours 2',
    ])
    policy = SenderChangePolicy(max_reposts=99, lose_reactions_ok=True)
    with pytest.raises(SenderChangeForbidden, match='reordering past foreign msg f1'):
        sync(client, desired, thread_id='t1', options=SyncOptions(sender_change=policy))


# --- policy gates ---

def test_sync_cascade_length_exceeds_max_reposts():
    """Cascade of 3 msgs against `max_reposts=2` → raise."""
    client = _Client({'t1': [
        _live('op', 'OP', sender_username='bot'),
        _live('r1', 'a', sender_username='old-name'),
        _live('r2', 'b', sender_username='bot'),
        _live('r3', 'c', sender_username='bot'),
    ]})
    desired = Thread(messages=[
        'OP',
        Msg(content='a', username='new-name'),
        'b', 'c',
    ])
    with pytest.raises(SenderChangeForbidden, match='touch 3 msgs.*max_reposts=2'):
        sync(client, desired, thread_id='t1',
             options=SyncOptions(sender_change=SenderChangePolicy(max_reposts=2)))


def test_sync_target_with_reactions_forbidden_by_default():
    """Any cascade member with reactions + `lose_reactions_ok=False` → raise."""
    client = _Client(
        {'t1': [
            _live('op', 'OP', sender_username='bot'),
            _live('r1', 'a', sender_username='old-name'),
        ]},
        reactions_by_id={'r1': [{'name': 'thumbsup', 'count': 3}]},
    )
    desired = Thread(messages=['OP', Msg(content='a', username='new-name')])
    with pytest.raises(SenderChangeForbidden, match=r"delete msgs with reactions: \['r1'\]"):
        sync(client, desired, thread_id='t1',
             options=SyncOptions(sender_change=SenderChangePolicy(max_reposts=5)))


def test_sync_lose_reactions_ok_bypasses_reactions_check():
    """`lose_reactions_ok=True` skips the reactions pre-flight → cascade proceeds."""
    client = _Client(
        {'t1': [
            _live('op', 'OP', sender_username='bot'),
            _live('r1', 'a', sender_username='old-name'),
        ]},
        reactions_by_id={'r1': [{'name': 'thumbsup', 'count': 3}]},
    )
    desired = Thread(messages=['OP', Msg(content='a', username='new-name')])
    result = sync(client, desired, thread_id='t1',
                  options=SyncOptions(sender_change=SenderChangePolicy(
                      max_reposts=5, lose_reactions_ok=True)))
    # Cascade actually ran: r1 deleted + reposted with new sender.
    assert 'r1' in client.delete_calls
    assert client.post_calls[-1]['username'] == 'new-name'


# --- cascade execution ---

def test_sync_reply_cascade_deletes_end_to_start_reposts_start_to_end():
    """Cascade of [r1, r2, r3] deletes r3→r2→r1 then posts r1→r2→r3."""
    client = _Client({'t1': [
        _live('op', 'OP', sender_username='bot'),
        _live('r1', 'a', sender_username='old-name'),
        _live('r2', 'b', sender_username='bot'),
        _live('r3', 'c', sender_username='bot'),
    ]})
    desired = Thread(messages=[
        'OP',
        Msg(content='a', username='new-name'),
        'b', 'c',
    ])
    result = sync(client, desired, thread_id='t1',
                  options=SyncOptions(sender_change=SenderChangePolicy(max_reposts=5)))
    # Deletes end-to-start.
    assert client.delete_calls == ['r3', 'r2', 'r1']
    # Reposts start-to-end. Only the target (index 1) got new_name; others
    # preserved their prior sender (which was None or 'bot' — both fine here).
    posts = [c['content'] for c in client.post_calls]
    assert posts == ['a', 'b', 'c']
    usernames = [c['username'] for c in client.post_calls]
    # Target repost carries the new name from the Msg; the other two are
    # bare `str` desired entries so they pass no sender kwargs.
    assert usernames == ['new-name', None, None]


def test_sync_dry_run_cascade_no_side_effects():
    """`dry_run=True` records DELETE + POST actions but calls no API."""
    client = _Client({'t1': [
        _live('op', 'OP', sender_username='bot'),
        _live('r1', 'a', sender_username='old-name'),
    ]})
    desired = Thread(messages=['OP', Msg(content='a', username='new-name')])
    result = sync(client, desired, thread_id='t1',
                  options=SyncOptions(
                      sender_change=SenderChangePolicy(max_reposts=5),
                      dry_run=True))
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.SKIP, ActionType.DELETE, ActionType.POST]
    assert client.delete_calls == []
    assert client.post_calls == []


# --- detection subtleties ---

def test_sync_no_mismatch_when_live_sender_fields_unknown():
    """`Message` with all sender fields None (non-Slack clients) → no cascade fires."""
    client = _Client({'t1': [
        Message(id='op', content='OP'),
        Message(id='r1', content='a'),
        # No sender_* fields = None → can't verify → treat as no mismatch.
    ]})
    desired = Thread(messages=['OP', Msg(content='a', username='X')])
    # Should NOT raise — mismatch check bails on unknown live sender.
    result = sync(client, desired, thread_id='t1')
    assert [a.type for a in result.actions] == [ActionType.SKIP, ActionType.SKIP]


def test_sync_icon_emoji_desired_skips_icon_check_no_false_positive():
    """Desired `icon_emoji` (not `icon_url`) → skip icon comparison entirely
    (Slack doesn't preserve emoji-vs-URL on read; unverifiable).

    Ensures we don't fire a false cascade just because live's `sender_icon_url`
    is a rendered emoji URL and desired's `d_url` is None. Username still
    compared (must match) — this test isolates the icon-check-skip behavior.
    """
    client = _Client(
        {'t1': [
            _live('op', 'OP', sender_username='bot', sender_icon_url='https://emoji-rendered.png'),
            _live('r1', 'a', sender_username='bot', sender_icon_url='https://emoji-rendered.png'),
        ]},
        username='bot',   # matches live so username check passes
    )
    desired = Thread(messages=[
        'OP',
        Msg(content='a', icon_emoji=':new_emoji:'),  # icon_emoji-only → icon check skipped
    ])
    # No raise — icon check skipped for emoji-desired.
    result = sync(client, desired, thread_id='t1')
    assert [a.type for a in result.actions] == [ActionType.SKIP, ActionType.SKIP]
