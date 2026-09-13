"""Tests for `DiscordWebhookClient` — the per-message-sender transport.

A webhook sets `username`/`avatar_url` per message (the bot API can't) but can't
open a thread or read messages, so this client is write-only (post/edit/delete)
and pairs with a `DiscordClient` for threading + reconcile. See
specs/discord-push.md.

The stub records each `_curl_raw` call as `(method, url, data, label)` so tests
assert exact request shapes; POSTs return a canned id. The fake `webhook_url`
below stands in for the real secret one.
"""
from __future__ import annotations

import pytest

from thrds import DiscordWebhookClient, Message

WEBHOOK = "https://discord.com/api/webhooks/123/faketoken"
AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"


class _RawRecorder:
    """Stub for `DiscordWebhookClient._curl_raw`: records calls, returns ids.

    A POST (message execution) returns a canned ``{"id": …}``; PATCH/DELETE
    return None (edit/delete ignore the body).
    """
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self._n = 0

    def __call__(self, method, url, data=None, *, headers=None, label=None):
        self.calls.append((method, url, data, label))
        if method == "POST":
            self._n += 1
            return {"id": f"w{self._n}"}
        return None


@pytest.fixture
def rec(monkeypatch):
    r = _RawRecorder()
    monkeypatch.setattr(
        DiscordWebhookClient, "_curl_raw",
        lambda self, method, url, data=None, *, headers=None, label=None: r(
            method, url, data, headers=headers, label=label,
        ),
    )
    return r


def test_post_sets_wait_and_per_message_sender(rec):
    client = DiscordWebhookClient(WEBHOOK)
    msg = client.post("hello", username="Alice", icon_url=AVATAR)
    assert msg == Message(id="w1", content="hello")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true",
         {"content": "hello", "username": "Alice", "avatar_url": AVATAR},
         "POST webhook message"),
    ]


def test_post_into_thread_appends_thread_id(rec):
    client = DiscordWebhookClient(WEBHOOK, thread_id="t9")
    client.post("in thread", username="Bob")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true&thread_id=t9",
         {"content": "in thread", "username": "Bob"},
         "POST webhook message"),
    ]


def test_post_per_call_thread_id_overrides_client_default(rec):
    client = DiscordWebhookClient(WEBHOOK, thread_id="t9")
    client.post("elsewhere", thread_id="t42")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true&thread_id=t42",
         {"content": "elsewhere"},
         "POST webhook message"),
    ]


def test_post_uses_client_default_sender(rec):
    client = DiscordWebhookClient(WEBHOOK, username="Digest", avatar_url=AVATAR)
    client.post("body")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true",
         {"content": "body", "username": "Digest", "avatar_url": AVATAR},
         "POST webhook message"),
    ]


def test_post_message_username_overrides_client_default(rec):
    client = DiscordWebhookClient(WEBHOOK, username="Digest", avatar_url=AVATAR)
    client.post("body", username="Alice")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true",
         {"content": "body", "username": "Alice", "avatar_url": AVATAR},
         "POST webhook message"),
    ]


def test_post_suppress_embeds_sets_flags(rec):
    client = DiscordWebhookClient(WEBHOOK, suppress_embeds=True)
    client.post("body")
    assert rec.calls == [
        ("POST", f"{WEBHOOK}?wait=true",
         {"content": "body", "flags": 4},
         "POST webhook message"),
    ]


def test_post_icon_emoji_raises(rec):
    client = DiscordWebhookClient(WEBHOOK)
    with pytest.raises(NotImplementedError, match="no emoji avatar"):
        client.post("body", icon_emoji=":tada:")
    assert rec.calls == []


def test_post_over_length_raises(rec):
    client = DiscordWebhookClient(WEBHOOK)
    with pytest.raises(ValueError, match="2000 char limit"):
        client.post("x" * 2001)
    assert rec.calls == []


def test_edit_patches_message_in_thread(rec):
    client = DiscordWebhookClient(WEBHOOK, thread_id="t9")
    msg = client.edit("w1", "edited")
    assert msg == Message(id="w1", content="edited")
    assert rec.calls == [
        ("PATCH", f"{WEBHOOK}/messages/w1?thread_id=t9",
         {"content": "edited"},
         "PATCH webhook message w1"),
    ]


def test_edit_without_thread_has_no_query(rec):
    client = DiscordWebhookClient(WEBHOOK)
    client.edit("w1", "edited")
    assert rec.calls == [
        ("PATCH", f"{WEBHOOK}/messages/w1",
         {"content": "edited"},
         "PATCH webhook message w1"),
    ]


def test_delete_targets_message_in_thread(rec):
    client = DiscordWebhookClient(WEBHOOK, thread_id="t9")
    client.delete("w1")
    assert rec.calls == [
        ("DELETE", f"{WEBHOOK}/messages/w1?thread_id=t9", None,
         "DELETE webhook message w1"),
    ]


def test_list_messages_raises(rec):
    client = DiscordWebhookClient(WEBHOOK)
    with pytest.raises(NotImplementedError, match="can't read messages"):
        client.list_messages("t9")
    assert rec.calls == []


def test_error_labels_never_carry_the_secret_url(rec):
    """Every request labels itself with method + message id, never the webhook
    URL — its token is a secret and error strings surface labels verbatim."""
    client = DiscordWebhookClient(WEBHOOK, thread_id="t9")
    client.post("a", username="Alice")
    client.edit("w1", "b")
    client.delete("w1")
    labels = [label for _, _, _, label in rec.calls]
    assert labels == [
        "POST webhook message",
        "PATCH webhook message w1",
        "DELETE webhook message w1",
    ]
    assert all(WEBHOOK not in label for label in labels)
