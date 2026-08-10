"""Python-specific sync tests.

Cross-language algorithm cases live in tests/fixtures/sync.json and are
exercised by test_fixtures.py. This file holds tests that depend on
Python surface: `EditRateLimited` exception handling, `Action.format`
ANSI escapes, `SyncOptions(dry_run=True)` semantics, and the
`DiscordClient` `Bot ` token prefix.
"""
from thrds import Action, ActionType, EditRateLimited, Message, Msg, SyncOptions, Thread, sync


class MockClient:
    """Minimal in-memory client used by tests below; for cross-language
    behavior assertions use `FixtureClient` in test_fixtures.py."""
    def __init__(self, threads: dict[str, list[Message]] | None = None):
        self.threads: dict[str, list[Message]] = threads or {}
        self._next_id = 1
        # Recorded post() calls for sender-forwarding assertions
        self.post_calls: list[dict] = []

    def _new_id(self) -> str:
        id_ = str(self._next_id)
        self._next_id += 1
        return id_

    def list_messages(self, thread_id: str) -> list[Message]:
        return list(self.threads.get(thread_id, []))

    def post(
        self,
        content: str,
        thread_id: str | None = None,
        *,
        username: str | None = None,
        icon_url: str | None = None,
        icon_emoji: str | None = None,
    ) -> Message:
        self.post_calls.append({
            'content': content,
            'thread_id': thread_id,
            'username': username,
            'icon_url': icon_url,
            'icon_emoji': icon_emoji,
        })
        msg = Message(id=self._new_id(), content=content)
        if thread_id is None:
            self.threads[msg.id] = [msg]
        else:
            self.threads.setdefault(thread_id, []).append(msg)
        return msg

    def edit(self, message_id: str, content: str) -> Message:
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    msgs[i] = Message(id=message_id, content=content)
                    return msgs[i]
        raise ValueError(f"Message {message_id} not found")

    def delete(self, message_id: str) -> None:
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    msgs.pop(i)
                    return
        raise ValueError(f"Message {message_id} not found")


def test_dry_run():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
    ]})
    desired = Thread(messages=["New OP", "Reply"])
    result = sync(client, desired, thread_id="t1", options=SyncOptions(dry_run=True))
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.EDIT, ActionType.POST]
    # No actual mutation occurred
    msgs = client.threads["t1"]
    assert [m.content for m in msgs] == ["OP"]


class RateLimitedMockClient(MockClient):
    """MockClient that raises EditRateLimited on the Nth edit call."""
    def __init__(self, fail_after_n_edits: int, **kwargs):
        super().__init__(**kwargs)
        self._edit_count = 0
        self._fail_after = fail_after_n_edits

    def edit(self, message_id: str, content: str) -> Message:
        self._edit_count += 1
        if self._edit_count > self._fail_after:
            raise EditRateLimited("rate limited")
        return super().edit(message_id, content)


def test_edit_rate_limit_fallback():
    """When edit hits rate limit, remaining messages are deleted and reposted."""
    client = RateLimitedMockClient(
        fail_after_n_edits=1,
        threads={"t1": [
            Message(id="m1", content="Old OP"),
            Message(id="m2", content="Old R1"),
            Message(id="m3", content="Old R2"),
        ]},
    )
    desired = Thread(messages=["New OP", "New R1", "New R2"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    # First edit succeeds, second triggers rate limit →
    # delete m3, m2 (backwards), then post New R1, New R2
    assert action_types == [
        ActionType.EDIT,    # m1 → "New OP" (succeeds)
        ActionType.EDIT,    # m2 → "New R1" (fails, triggers fallback)
        ActionType.DELETE,  # m3
        ActionType.DELETE,  # m2
        ActionType.POST,    # "New R1"
        ActionType.POST,    # "New R2"
    ]
    assert len(result.message_ids) == 3
    # First ID is m1 (edited), other two are new
    assert result.message_ids[0] == "m1"


def test_edit_rate_limit_on_first_edit():
    """Rate limit on the very first edit → delete all, repost all."""
    client = RateLimitedMockClient(
        fail_after_n_edits=0,
        threads={"t1": [
            Message(id="m1", content="Old OP"),
            Message(id="m2", content="Old R1"),
        ]},
    )
    desired = Thread(messages=["New OP", "New R1"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [
        ActionType.EDIT,    # m1 (fails)
        ActionType.DELETE,  # m2
        ActionType.DELETE,  # m1
        ActionType.POST,    # "New OP"
        ActionType.POST,    # "New R1"
    ]
    assert len(result.message_ids) == 2


def test_bot_token_prefix():
    """DiscordClient auto-prepends 'Bot ' to tokens."""
    from thrds import DiscordClient
    client_bare = DiscordClient(token="my-token", channel_id="123")
    assert client_bare.token == "Bot my-token"
    client_prefixed = DiscordClient(token="Bot my-token", channel_id="123")
    assert client_prefixed.token == "Bot my-token"


def test_action_format_no_color():
    """Action.format(color=False) produces plain unified-diff output."""
    post = Action(type=ActionType.POST, index=0, content="hello")
    assert post.format(color=False) == "POST [0]\n  +hello"

    edit = Action(type=ActionType.EDIT, index=1, message_id="x", content="new", prior_content="old")
    assert edit.format(color=False) == "EDIT [1]\n  -old\n  +new"

    delete = Action(type=ActionType.DELETE, index=2, message_id="y", prior_content="gone")
    assert delete.format(color=False) == "DELETE [2]\n  -gone"

    skip = Action(type=ActionType.SKIP, index=3, message_id="z", content="same")
    assert skip.format(color=False) == "SKIP [3] (unchanged)"


def test_action_format_multiline():
    """Multi-line content gets the +/- prefix on every line."""
    edit = Action(
        type=ActionType.EDIT,
        index=0,
        message_id="x",
        content="line 1\nline 2",
        prior_content="old 1\nold 2",
    )
    assert edit.format(color=False) == "EDIT [0]\n  -old 1\n  -old 2\n  +line 1\n  +line 2"


def test_action_format_color():
    """Action.format(color=True) wraps content in ANSI codes."""
    RED, GREEN, RESET = "\033[31m", "\033[32m", "\033[0m"
    edit = Action(type=ActionType.EDIT, index=0, message_id="x", content="new", prior_content="old")
    assert edit.format(color=True) == f"EDIT [0]\n  {RED}-old{RESET}\n  {GREEN}+new{RESET}"


def test_sync_result_format_preview():
    """SyncResult.format_preview aggregates all actions with optional prefix."""
    client = MockClient({
        "t1": [
            Message(id="m0", content="OP"),
            Message(id="m1", content="old"),
        ],
    })
    desired = Thread(messages=["OP", "new"])
    result = sync(client, desired, thread_id="t1")

    preview = result.format_preview(color=False)
    assert preview == "SKIP [0] (unchanged)\nEDIT [1]\n  -old\n  +new"

    prefixed = result.format_preview(color=False, prefix="t1: ")
    assert prefixed == "t1: SKIP [0] (unchanged)\nt1: EDIT [1]\nt1:   -old\nt1:   +new"


# --- Msg wrapper / per-message sender ---

def test_sync_msg_entries_carry_sender_to_post():
    """A `Msg` entry in `Thread.messages` forwards username/icon_url/icon_emoji to `post()`."""
    client = MockClient()
    desired = Thread(messages=[
        Msg(content="OP text", username="Custom OP", icon_url="https://cdn.example/op.png"),
        Msg(content="Reply text", username="Custom Reply", icon_emoji=":sparkles:"),
    ])
    sync(client, desired)
    assert client.post_calls == [
        {'content': 'OP text', 'thread_id': None,
         'username': 'Custom OP', 'icon_url': 'https://cdn.example/op.png', 'icon_emoji': None},
        # After the OP is posted, its ID becomes the thread_id for subsequent replies.
        {'content': 'Reply text', 'thread_id': '1',
         'username': 'Custom Reply', 'icon_url': None, 'icon_emoji': ':sparkles:'},
    ]


def test_sync_str_entries_send_no_sender_fields():
    """Bare `str` entries send None for every sender field (client defaults kick in downstream)."""
    client = MockClient()
    sync(client, Thread(messages=["OP", "Reply"]))
    for call in client.post_calls:
        assert call['username'] is None
        assert call['icon_url'] is None
        assert call['icon_emoji'] is None


def test_sync_mixed_str_and_msg_entries():
    """Mixing `str` and `Msg` in one thread is fine — each entry resolves independently."""
    client = MockClient()
    desired = Thread(messages=[
        Msg(content="OP", username="Digest — 2026-08-10"),
        "Reply 1 (plain str)",
        Msg(content="Reply 2 (Msg)", username="Digest"),
    ])
    sync(client, desired)
    usernames = [c['username'] for c in client.post_calls]
    assert usernames == ['Digest — 2026-08-10', None, 'Digest']


def test_sync_edit_does_not_carry_sender_fields():
    """Changing content in a `Msg` triggers EDIT — sender is post-time-only, so
    the edit path uses `client.edit(id, content)` with NO sender forwarded.
    (MockClient.edit signature only takes id+content; if sync tried to pass
    sender kwargs, this would raise TypeError.)"""
    client = MockClient({"t1": [Message(id="m1", content="original")]})
    desired = Thread(messages=[
        Msg(content="edited", username="new-name", icon_url="https://cdn.example/x.png"),
    ])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.EDIT]
    # No POST calls happened — proves sender fields didn't leak into edit path.
    assert client.post_calls == []


def test_sync_msg_content_powers_positional_diff():
    """The positional diff compares `Msg.content` (not the whole Msg) — a sender-only
    change on an unchanged content is a SKIP, not an EDIT."""
    client = MockClient({"t1": [Message(id="m1", content="same")]})
    # `Msg(content='same', ...)` has same content as existing → SKIP.
    desired = Thread(messages=[Msg(content="same", username="new-sender")])
    result = sync(client, desired, thread_id="t1")
    assert [a.type for a in result.actions] == [ActionType.SKIP]
    assert client.post_calls == []
