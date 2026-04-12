from thrds import Action, ActionType, EditRateLimited, Message, SyncOptions, Thread, sync


class MockClient:
    """In-memory ThreadClient for testing."""
    def __init__(self, threads: dict[str, list[Message]] | None = None):
        self.threads: dict[str, list[Message]] = threads or {}
        self._next_id = 1

    def _new_id(self) -> str:
        id_ = str(self._next_id)
        self._next_id += 1
        return id_

    def list_messages(self, thread_id: str) -> list[Message]:
        return list(self.threads.get(thread_id, []))

    def post(self, content: str, thread_id: str | None = None) -> Message:
        msg = Message(id=self._new_id(), content=content)
        if thread_id is None:
            # Creating a new thread; use message id as thread id
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


def test_create_new_thread():
    client = MockClient()
    desired = Thread(messages=["OP", "Reply 1", "Reply 2"])
    result = sync(client, desired)
    assert len(result.message_ids) == 3
    assert result.thread_id == result.message_ids[0]
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.POST, ActionType.POST, ActionType.POST]
    # Verify state
    msgs = client.threads[result.thread_id]
    assert [m.content for m in msgs] == ["OP", "Reply 1", "Reply 2"]


def test_no_changes():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
        Message(id="m2", content="Reply"),
    ]})
    desired = Thread(messages=["OP", "Reply"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.SKIP, ActionType.SKIP]
    assert result.message_ids == ["m1", "m2"]


def test_edit_existing():
    client = MockClient({"t1": [
        Message(id="m1", content="Old OP"),
        Message(id="m2", content="Old Reply"),
    ]})
    desired = Thread(messages=["New OP", "New Reply"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.EDIT, ActionType.EDIT]
    msgs = client.threads["t1"]
    assert [m.content for m in msgs] == ["New OP", "New Reply"]


def test_append_new_replies():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
    ]})
    desired = Thread(messages=["OP", "Reply 1", "Reply 2"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.SKIP, ActionType.POST, ActionType.POST]
    assert len(result.message_ids) == 3


def test_delete_extras():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
        Message(id="m2", content="Reply 1"),
        Message(id="m3", content="Reply 2"),
    ]})
    desired = Thread(messages=["OP"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    # Deletes happen backwards (m3, m2), then skip for OP
    assert action_types == [ActionType.DELETE, ActionType.DELETE, ActionType.SKIP]
    msgs = client.threads["t1"]
    assert [m.content for m in msgs] == ["OP"]


def test_delete_all():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
        Message(id="m2", content="Reply"),
    ]})
    desired = Thread(messages=[])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    # Delete reply first, then OP
    assert action_types == [ActionType.DELETE, ActionType.DELETE]
    msgs = client.threads["t1"]
    assert msgs == []


def test_mixed_edit_and_append():
    client = MockClient({"t1": [
        Message(id="m1", content="Old OP"),
    ]})
    desired = Thread(messages=["New OP", "New Reply"])
    result = sync(client, desired, thread_id="t1")
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.EDIT, ActionType.POST]


def test_dry_run():
    client = MockClient({"t1": [
        Message(id="m1", content="OP"),
    ]})
    desired = Thread(messages=["New OP", "Reply"])
    result = sync(client, desired, thread_id="t1", options=SyncOptions(dry_run=True))
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.EDIT, ActionType.POST]
    # Verify no actual changes were made
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


def test_action_prior_content_populated():
    """sync() populates prior_content on EDIT and DELETE actions."""
    client = MockClient({
        "t1": [
            Message(id="m0", content="OP"),
            Message(id="m1", content="old reply"),
            Message(id="m2", content="to delete"),
        ],
    })
    desired = Thread(messages=["OP", "new reply"])
    result = sync(client, desired, thread_id="t1")

    edit_action = next(a for a in result.actions if a.type is ActionType.EDIT)
    assert edit_action.prior_content == "old reply"
    assert edit_action.content == "new reply"

    delete_action = next(a for a in result.actions if a.type is ActionType.DELETE)
    assert delete_action.prior_content == "to delete"

    skip_action = next(a for a in result.actions if a.type is ActionType.SKIP)
    assert skip_action.prior_content is None


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
