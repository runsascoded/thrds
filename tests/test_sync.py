from thrds import Action, ActionType, Message, SyncOptions, Thread, sync


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
