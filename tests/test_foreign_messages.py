"""Non-editable (foreign) messages — e.g. human interjections in a
bot-managed thread — must be preserved in place. The sync reconcile
should treat them as invisible: never edit, never delete, and never
count them against the desired message slots.
"""
from thrds import ActionType, Message, Thread, sync


class MockClient:
    def __init__(self, threads: dict[str, list[Message]] | None = None):
        self.threads: dict[str, list[Message]] = threads or {}
        self._next_id = 100
        self.edit_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.post_calls: list[tuple[str, str | None]] = []

    def _new_id(self) -> str:
        id_ = str(self._next_id)
        self._next_id += 1
        return id_

    def list_messages(self, thread_id: str) -> list[Message]:
        return list(self.threads.get(thread_id, []))

    def post(self, content: str, thread_id: str | None = None) -> Message:
        self.post_calls.append((content, thread_id))
        msg = Message(id=self._new_id(), content=content, editable=True)
        if thread_id is None:
            self.threads[msg.id] = [msg]
        else:
            self.threads.setdefault(thread_id, []).append(msg)
        return msg

    def edit(self, message_id: str, content: str) -> Message:
        self.edit_calls.append((message_id, content))
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    assert m.editable, f"edit() called on non-editable {message_id}"
                    msgs[i] = Message(id=message_id, content=content, editable=True)
                    return msgs[i]
        raise ValueError(f"Message {message_id} not found")

    def delete(self, message_id: str) -> None:
        self.delete_calls.append(message_id)
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    assert m.editable, f"delete() called on non-editable {message_id}"
                    msgs.pop(i)
                    return
        raise ValueError(f"Message {message_id} not found")


def test_human_interjected_edits_bot_slots_only():
    """Thread [bot, bot, human, bot], desired [bot', bot', bot']:
    edit all three bot messages (via their ids), leave human alone."""
    client = MockClient({"t1": [
        Message(id="b1", content="bot-0-old", editable=True),
        Message(id="b2", content="bot-1-old", editable=True),
        Message(id="h1", content="human-link", editable=False),
        Message(id="b3", content="bot-2-old", editable=True),
    ]})
    desired = Thread(messages=["bot-0-new", "bot-1-new", "bot-2-new"])
    result = sync(client, desired, thread_id="t1")

    assert [a.type for a in result.actions] == [ActionType.EDIT, ActionType.EDIT, ActionType.EDIT]
    assert client.edit_calls == [("b1", "bot-0-new"), ("b2", "bot-1-new"), ("b3", "bot-2-new")]
    assert client.delete_calls == []
    assert client.post_calls == []
    # Human message still in thread, content intact
    thread = client.threads["t1"]
    human = [m for m in thread if m.id == "h1"]
    assert len(human) == 1
    assert human[0].content == "human-link"
    assert not human[0].editable


def test_shrink_bot_slots_preserves_human():
    """Thread [bot, bot, human, bot], desired [bot']:
    edit slot 0, delete editable extras (b2, b3), leave human alone."""
    client = MockClient({"t1": [
        Message(id="b1", content="bot-0-old", editable=True),
        Message(id="b2", content="bot-1-old", editable=True),
        Message(id="h1", content="human-link", editable=False),
        Message(id="b3", content="bot-2-old", editable=True),
    ]})
    desired = Thread(messages=["bot-0-new"])
    result = sync(client, desired, thread_id="t1")

    # Phase 1 deletes editable tail (b3, b2), Phase 2 edits b1
    assert client.delete_calls == ["b3", "b2"]
    assert client.edit_calls == [("b1", "bot-0-new")]
    assert client.post_calls == []
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.DELETE, ActionType.DELETE, ActionType.EDIT]
    # Human message still in thread
    ids = [m.id for m in client.threads["t1"]]
    assert "h1" in ids
    assert "b2" not in ids and "b3" not in ids


def test_grow_appends_after_human():
    """Thread [bot, human], desired [bot, bot2]:
    skip bot (nop), post bot2 at thread end (after the human)."""
    client = MockClient({"t1": [
        Message(id="b1", content="bot-0", editable=True),
        Message(id="h1", content="human-link", editable=False),
    ]})
    desired = Thread(messages=["bot-0", "bot-1"])
    result = sync(client, desired, thread_id="t1")

    assert client.edit_calls == []
    assert client.delete_calls == []
    assert client.post_calls == [("bot-1", "t1")]
    action_types = [a.type for a in result.actions]
    assert action_types == [ActionType.SKIP, ActionType.POST]
    # Thread ends: [b1, h1, new bot-1]
    contents = [m.content for m in client.threads["t1"]]
    assert contents == ["bot-0", "human-link", "bot-1"]


def test_human_at_start_edits_bot_slots():
    """Thread [human, bot, human], desired [bot']:
    edit the one bot message; both humans untouched."""
    client = MockClient({"t1": [
        Message(id="h1", content="human-0", editable=False),
        Message(id="b1", content="bot-0-old", editable=True),
        Message(id="h2", content="human-2", editable=False),
    ]})
    desired = Thread(messages=["bot-0-new"])
    result = sync(client, desired, thread_id="t1")

    assert client.edit_calls == [("b1", "bot-0-new")]
    assert client.delete_calls == []
    assert client.post_calls == []
    assert [a.type for a in result.actions] == [ActionType.EDIT]
    ids = [m.id for m in client.threads["t1"]]
    assert ids == ["h1", "b1", "h2"]


def test_all_human_thread_posts_new_bot_message():
    """Thread [human], desired [bot]:
    no editable slots; post bot as new message at thread end."""
    client = MockClient({"t1": [
        Message(id="h1", content="human-only", editable=False),
    ]})
    desired = Thread(messages=["bot-0"])
    result = sync(client, desired, thread_id="t1")

    assert client.edit_calls == []
    assert client.delete_calls == []
    assert client.post_calls == [("bot-0", "t1")]
    assert [a.type for a in result.actions] == [ActionType.POST]
    contents = [m.content for m in client.threads["t1"]]
    assert contents == ["human-only", "bot-0"]
