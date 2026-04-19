import pytest

from thrds import OrphanedRepliesError
from thrds.slack import SlackClient


class FakeSlackClient(SlackClient):
    """SlackClient with stubbed _request for testing orphan guard."""

    def __init__(self, replies_by_ts: dict[str, list[dict]]):
        super().__init__(token="xoxb-fake", channel="C123")
        self._replies_by_ts = replies_by_ts
        self.deleted: list[str] = []

    def _request(self, endpoint: str, data: dict | None = None, method: str = "POST") -> dict:
        if endpoint == "conversations.replies":
            ts = data["ts"]
            return {"ok": True, "messages": self._replies_by_ts.get(ts, [])}
        if endpoint == "chat.delete":
            self.deleted.append(data["ts"])
            return {"ok": True}
        raise RuntimeError(f"Unexpected endpoint: {endpoint}")


def test_delete_blocks_when_message_has_replies():
    """delete() raises OrphanedRepliesError for messages with thread replies."""
    client = FakeSlackClient(replies_by_ts={
        "1000.0": [
            {"ts": "1000.0", "text": "parent"},
            {"ts": "1001.0", "text": "reply 1"},
            {"ts": "1002.0", "text": "reply 2"},
        ],
    })
    with pytest.raises(OrphanedRepliesError) as exc_info:
        client.delete("1000.0")
    assert exc_info.value.message_id == "1000.0"
    assert exc_info.value.reply_count == 2
    assert client.deleted == []


def test_delete_allows_when_no_replies():
    """delete() proceeds when the message has no thread replies."""
    client = FakeSlackClient(replies_by_ts={
        "1000.0": [
            {"ts": "1000.0", "text": "just me"},
        ],
    })
    client.delete("1000.0")
    assert client.deleted == ["1000.0"]


def test_delete_orphans_ok_bypasses_check():
    """delete(orphans_ok=True) skips the replies check."""
    client = FakeSlackClient(replies_by_ts={
        "1000.0": [
            {"ts": "1000.0", "text": "parent"},
            {"ts": "1001.0", "text": "reply"},
        ],
    })
    client.delete("1000.0", orphans_ok=True)
    assert client.deleted == ["1000.0"]
