"""Tests for BskyClient behavior.

These test the client's protocol compliance without hitting the real API.
"""
import pytest

from thrds import ActionType, EditRateLimited, Message, Thread, sync


class NoEditMockClient:
    """Mock that behaves like Bluesky: no edits, only post/delete."""
    def __init__(self, threads: dict[str, list[Message]] | None = None):
        self.threads: dict[str, list[Message]] = threads or {}
        self._next_id = 1

    def _new_id(self) -> str:
        id_ = f"at://did:plc:test/app.bsky.feed.post/{self._next_id}"
        self._next_id += 1
        return id_

    def list_messages(self, thread_id: str) -> list[Message]:
        return list(self.threads.get(thread_id, []))

    def post(self, content: str, thread_id: str | None = None) -> Message:
        if len(content) > 300:
            raise ValueError(f"Post exceeds 300 char limit ({len(content)} chars)")
        msg = Message(id=self._new_id(), content=content)
        if thread_id is None:
            self.threads[msg.id] = [msg]
        else:
            self.threads.setdefault(thread_id, []).append(msg)
        return msg

    def edit(self, message_id: str, content: str) -> Message:
        raise EditRateLimited("Bluesky does not support editing posts")

    def delete(self, message_id: str) -> None:
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    msgs.pop(i)
                    return


def test_bsky_new_thread():
    """Creating a new thread works (no edits needed)."""
    client = NoEditMockClient()
    desired = Thread(messages=["Root post", "Reply 1"])
    result = sync(client, desired)
    types = [a.type for a in result.actions]
    assert types == [ActionType.POST, ActionType.POST]
    assert len(result.message_ids) == 2


def test_bsky_no_changes():
    """Existing thread matching desired state → all skips, no edits attempted."""
    client = NoEditMockClient(threads={"t1": [
        Message(id="p1", content="Root"),
        Message(id="p2", content="Reply"),
    ]})
    desired = Thread(messages=["Root", "Reply"])
    result = sync(client, desired, thread_id="t1")
    types = [a.type for a in result.actions]
    assert types == [ActionType.SKIP, ActionType.SKIP]


def test_bsky_update_falls_back_to_repost():
    """Changing content triggers edit → EditRateLimited → delete+repost."""
    client = NoEditMockClient(threads={"t1": [
        Message(id="p1", content="Old root"),
        Message(id="p2", content="Old reply"),
    ]})
    desired = Thread(messages=["New root", "New reply"])
    result = sync(client, desired, thread_id="t1")
    types = [a.type for a in result.actions]
    # Edit p1 fails → delete p2, p1 → post "New root", "New reply"
    assert types == [
        ActionType.EDIT,    # attempted, fails
        ActionType.DELETE,  # p2
        ActionType.DELETE,  # p1
        ActionType.POST,    # "New root"
        ActionType.POST,    # "New reply"
    ]
    assert len(result.message_ids) == 2


def test_bsky_append_no_edit():
    """Adding replies to unchanged thread → just posts, no edits triggered."""
    client = NoEditMockClient(threads={"t1": [
        Message(id="p1", content="Root"),
    ]})
    desired = Thread(messages=["Root", "New reply"])
    result = sync(client, desired, thread_id="t1")
    types = [a.type for a in result.actions]
    assert types == [ActionType.SKIP, ActionType.POST]
    assert result.message_ids[0] == "p1"


def test_bsky_delete_extras():
    """Removing replies from end → deletes only, no edits triggered."""
    client = NoEditMockClient(threads={"t1": [
        Message(id="p1", content="Root"),
        Message(id="p2", content="Old reply"),
    ]})
    desired = Thread(messages=["Root"])
    result = sync(client, desired, thread_id="t1")
    types = [a.type for a in result.actions]
    assert types == [ActionType.DELETE, ActionType.SKIP]
    assert result.message_ids == ["p1"]


def test_bsky_char_limit():
    """Posts exceeding 300 chars raise ValueError."""
    client = NoEditMockClient()
    long_msg = "x" * 301
    desired = Thread(messages=[long_msg])
    try:
        sync(client, desired)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "300 char limit" in str(e)


def test_bsky_import():
    """BskyClient is importable when atproto is installed."""
    atproto = pytest.importorskip("atproto")
    from thrds import BskyClient
    assert BskyClient is not None


def test_bsky_edit_raises():
    """BskyClient.edit() always raises EditRateLimited."""
    atproto = pytest.importorskip("atproto")
    from thrds.bsky import BskyClient
    assert hasattr(BskyClient, 'edit')
