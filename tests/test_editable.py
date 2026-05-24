"""Regression: `list_messages` must mark our own bot's posts as `editable`.

Slack returns `user: null` on `bot_message` events; the bot's identity
lives in `bot_id`. Matching only on `user_id` mis-labels every one of
our own posts as non-editable, which in turn makes `sync()` see an empty
existing thread and re-post all `desired` messages every run.
"""
from thrds.slack import SlackClient


class FakeSlackClient(SlackClient):
    """SlackClient with stubbed `_request` driving `auth.test` and
    `conversations.replies` from in-memory dicts.
    """

    def __init__(self, auth: dict, replies_by_ts: dict[str, list[dict]]):
        super().__init__(token="xoxb-fake", channel="C123")
        self._auth = auth
        self._replies_by_ts = replies_by_ts

    def _request(self, endpoint: str, data: dict | None = None, method: str = "POST") -> dict:
        if endpoint == "auth.test":
            return {"ok": True, **self._auth}
        if endpoint == "conversations.replies":
            ts = data["ts"]
            return {"ok": True, "messages": self._replies_by_ts.get(ts, [])}
        raise RuntimeError(f"Unexpected endpoint: {endpoint}")


def test_list_messages_marks_bot_message_editable():
    """A `bot_message` (user=null, bot_id=ours) is editable."""
    client = FakeSlackClient(
        auth={"user_id": "U_BOT", "bot_id": "B_BOT"},
        replies_by_ts={"1000.0": [
            {"ts": "1000.0", "text": "from bot", "user": None, "bot_id": "B_BOT"},
        ]},
    )
    messages = client.list_messages("1000.0")
    assert [(m.id, m.content, m.editable) for m in messages] == [
        ("1000.0", "from bot", True),
    ]


def test_list_messages_marks_human_reply_non_editable():
    """A human reply (different user, no bot_id) is non-editable; our
    bot's OP stays editable."""
    client = FakeSlackClient(
        auth={"user_id": "U_BOT", "bot_id": "B_BOT"},
        replies_by_ts={"1000.0": [
            {"ts": "1000.0", "text": "OP", "user": None, "bot_id": "B_BOT"},
            {"ts": "1001.0", "text": "hi", "user": "U_HUMAN"},
        ]},
    )
    messages = client.list_messages("1000.0")
    assert [(m.id, m.editable) for m in messages] == [
        ("1000.0", True),
        ("1001.0", False),
    ]


def test_list_messages_matches_user_id_for_user_token_posts():
    """A message with `user == our user_id` (e.g. posted with a user
    token, not a bot token) is editable even without a `bot_id`."""
    client = FakeSlackClient(
        auth={"user_id": "U_ME", "bot_id": "B_BOT"},
        replies_by_ts={"1000.0": [
            {"ts": "1000.0", "text": "as user", "user": "U_ME"},
        ]},
    )
    messages = client.list_messages("1000.0")
    assert [(m.id, m.editable) for m in messages] == [("1000.0", True)]


def test_list_messages_marks_foreign_bot_non_editable():
    """A different bot's message (different `bot_id`) is non-editable."""
    client = FakeSlackClient(
        auth={"user_id": "U_BOT", "bot_id": "B_BOT"},
        replies_by_ts={"1000.0": [
            {"ts": "1000.0", "text": "from other bot", "user": None, "bot_id": "B_OTHER"},
        ]},
    )
    messages = client.list_messages("1000.0")
    assert [(m.id, m.editable) for m in messages] == [("1000.0", False)]


def test_bot_ids_resolved_once():
    """`auth.test` only fires once across repeated `list_messages` calls."""
    auth_calls = 0

    class CountingClient(FakeSlackClient):
        def _request(self, endpoint, data=None, method="POST"):
            nonlocal auth_calls
            if endpoint == "auth.test":
                auth_calls += 1
            return super()._request(endpoint, data, method)

    client = CountingClient(
        auth={"user_id": "U_BOT", "bot_id": "B_BOT"},
        replies_by_ts={"1000.0": [
            {"ts": "1000.0", "text": "x", "user": None, "bot_id": "B_BOT"},
        ]},
    )
    client.list_messages("1000.0")
    client.list_messages("1000.0")
    assert auth_calls == 1
