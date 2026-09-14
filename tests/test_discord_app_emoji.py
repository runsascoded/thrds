"""Discord application emoji + app-owned webhook bootstrap (`specs/discord-app-emoji.md`).

`DiscordClient.create_webhook` / `app_emojis` / `upload_app_emoji` let a consumer
bootstrap the hybrid pair and its emoji from a bot token alone. `_curl` is
stubbed onto a recorder so each test asserts the exact request shape.
"""
from __future__ import annotations

import base64

import pytest

from thrds import DiscordClient

APP = "app-9"


class _Rec:
    """Stub for `DiscordClient._curl`: records `(method, path, data)`, returns
    canned responses keyed by endpoint. `webhooks` seeds the channel's existing
    webhook list."""
    def __init__(self, webhooks=None, emojis=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._webhooks = webhooks or []
        self._emojis = emojis or []

    def __call__(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET" and path == "/applications/@me":
            return {"id": APP}
        if method == "GET" and path.endswith("/webhooks"):
            return self._webhooks
        if method == "POST" and path.endswith("/webhooks"):
            return {"id": "wh1", "token": "tok", "application_id": APP, "name": data["name"]}
        if method == "GET" and path.endswith("/emojis"):
            return {"items": self._emojis}
        if method == "POST" and path.endswith("/emojis"):
            return {"id": "e-new", "name": data["name"]}
        return None


def _client(monkeypatch, rec):
    monkeypatch.setattr(DiscordClient, "_curl", lambda self, m, p, data=None: rec(m, p, data))
    return DiscordClient("bot-tok", "CHAN", "GUILD")


def test_create_webhook_creates_when_absent(monkeypatch):
    rec = _Rec(webhooks=[])
    client = _client(monkeypatch, rec)
    url = client.create_webhook("gcs digest")
    assert url == "https://discord.com/api/webhooks/wh1/tok"
    # No existing webhooks → the reuse loop never runs, so no app-id lookup.
    assert rec.calls == [
        ("GET", "/channels/CHAN/webhooks", None),
        ("POST", "/channels/CHAN/webhooks", {"name": "gcs digest"}),
    ]


def test_create_webhook_reuses_app_owned_by_name(monkeypatch):
    rec = _Rec(webhooks=[{"id": "wh1", "token": "tok", "name": "gcs digest", "application_id": APP}])
    client = _client(monkeypatch, rec)
    url = client.create_webhook("gcs digest")
    assert url == "https://discord.com/api/webhooks/wh1/tok"
    # No POST — the existing app-owned webhook is reused (app-id fetched to verify).
    assert rec.calls == [
        ("GET", "/channels/CHAN/webhooks", None),
        ("GET", "/applications/@me", None),
    ]


def test_create_webhook_ignores_user_created_same_name(monkeypatch):
    # A user-created webhook (application_id null) of the same name can't post
    # app emoji, so it is NOT reused — a fresh app-owned one is created.
    rec = _Rec(webhooks=[{"id": "old", "token": "x", "name": "gcs digest", "application_id": None}])
    client = _client(monkeypatch, rec)
    url = client.create_webhook("gcs digest")
    assert url == "https://discord.com/api/webhooks/wh1/tok"
    assert rec.calls[-1] == ("POST", "/channels/CHAN/webhooks", {"name": "gcs digest"})


def test_create_webhook_passes_avatar(monkeypatch):
    rec = _Rec(webhooks=[])
    client = _client(monkeypatch, rec)
    client.create_webhook("d", avatar="data:image/png;base64,AAAA")
    assert rec.calls[-1] == (
        "POST", "/channels/CHAN/webhooks", {"name": "d", "avatar": "data:image/png;base64,AAAA"},
    )


def test_app_emojis_maps_name_to_id(monkeypatch):
    rec = _Rec(emojis=[{"name": "up", "id": 111}, {"name": "down", "id": 222}])
    client = _client(monkeypatch, rec)
    assert client.app_emojis() == {"up": "111", "down": "222"}
    assert rec.calls == [
        ("GET", "/applications/@me", None),
        ("GET", "/applications/app-9/emojis", None),
    ]


def test_upload_app_emoji_posts_data_uri(monkeypatch, tmp_path):
    png = tmp_path / "up.png"
    png.write_bytes(b"\x89PNGbytes")
    rec = _Rec()
    client = _client(monkeypatch, rec)
    eid = client.upload_app_emoji("arrow_up", png)
    assert eid == "e-new"
    expected_image = "data:image/png;base64," + base64.b64encode(b"\x89PNGbytes").decode("ascii")
    assert rec.calls == [
        ("GET", "/applications/@me", None),
        ("POST", "/applications/app-9/emojis", {"name": "arrow_up", "image": expected_image}),
    ]


def test_upload_app_emoji_rejects_hyphen_name(monkeypatch):
    rec = _Rec()
    client = _client(monkeypatch, rec)
    with pytest.raises(ValueError, match=r"must be \[A-Za-z0-9_\]\{2,32\}"):
        client.upload_app_emoji("arrow_deg-30", "/x/p.png")
    assert rec.calls == []
