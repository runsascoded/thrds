"""Regression tests for transport-layer robustness (specs/transport-robustness.md)."""
from __future__ import annotations

import subprocess

import pytest

import thrds.discord as discord_mod
from thrds import LinkedThread, Section
from thrds.core import EditRateLimited
from thrds.discord import DiscordClient
from thrds.slack import SlackClient


class _FakeSlackClient(SlackClient):
    """SlackClient with `_request` stubbed to a scripted dispatcher.

    Handlers are per-endpoint callables receiving (data, method) and returning a
    response dict (mirroring the real Slack API shape). Missing handlers raise.
    """
    def __init__(self, handlers):
        super().__init__(token="x", channel="C")
        self._handlers = handlers
        self.calls: list[tuple[str, dict | None, str]] = []

    def _request(self, endpoint, data=None, method="POST"):
        self.calls.append((endpoint, data, method))
        handler = self._handlers.get(endpoint)
        if handler is None:
            raise NotImplementedError(f"no handler for {endpoint}")
        return handler(data, method)


def test_slack_sync_linked_raises_on_phase4_count_mismatch():
    """Phase-4 repack producing != phase-1 count must raise; never silent zip truncation."""
    posts: list[str] = []
    edits: list[tuple[str, str]] = []
    ts_counter = iter(f"1.{i:06d}" for i in range(1000))

    def on_auth(_data, _method):
        return {"ok": True, "user_id": "U1", "bot_id": "B1"}

    def on_post(data, _method):
        ts = next(ts_counter)
        posts.append(data["text"])
        return {"ok": True, "ts": ts}

    def on_edit(data, _method):
        edits.append((data["ts"], data["text"]))
        return {"ok": True}

    def on_replies(_data, _method):
        return {"ok": True, "messages": []}

    # Real permalink pathologically longer than the 180-char placeholder,
    # so phase-4 real-URL packing must yield more messages than phase 1.
    def on_permalink(_data, _method):
        return {"ok": True, "permalink": "https://s.slack.com/x" + "z" * 400}

    handlers = {
        "auth.test": on_auth,
        "chat.postMessage": on_post,
        "chat.update": on_edit,
        "conversations.replies": on_replies,
        "chat.getPermalink": on_permalink,
    }
    client = _FakeSlackClient(handlers)

    linked = LinkedThread(
        summary_prefix="",
        sections=[
            Section(title=f"S{i}", summary="x" * 300, body=f"Detail {i}")
            for i in range(8)
        ],
    )
    with pytest.raises(RuntimeError, match=r"phase-4 repack yielded \d+ summary messages, phase-1 posted \d+"):
        client.sync_linked(linked, pace=0.0, jitter=0.0)


def test_slack_post_raises_on_over_limit():
    """`SlackClient.post` must guard against > SLACK_MESSAGE_LIMIT content."""
    from thrds.slack import SLACK_MESSAGE_LIMIT
    client = _FakeSlackClient({})
    with pytest.raises(ValueError, match=rf"exceeds Slack's {SLACK_MESSAGE_LIMIT} char limit"):
        client.post("x" * (SLACK_MESSAGE_LIMIT + 1))


def test_slack_edit_raises_on_over_limit():
    """`SlackClient.edit` must guard against > SLACK_MESSAGE_LIMIT content."""
    from thrds.slack import SLACK_MESSAGE_LIMIT
    client = _FakeSlackClient({})
    with pytest.raises(ValueError, match=rf"exceeds Slack's {SLACK_MESSAGE_LIMIT} char limit"):
        client.edit("1.000001", "x" * (SLACK_MESSAGE_LIMIT + 1))


def _mk_curl_result(body: str, status: int, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    """Simulate curl's `-w '\\n%{http_code}'` output shape."""
    stdout = f"{body}\n{status}" if returncode == 0 else ""
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def _no_sleep(monkeypatch):
    """Skip real backoff sleeps in retry tests."""
    monkeypatch.setattr(discord_mod.time, "sleep", lambda _: None)


def _stub_curl_sequence(monkeypatch, results: list[subprocess.CompletedProcess]) -> list[list[str]]:
    calls: list[list[str]] = []
    it = iter(results)
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return next(it)
    monkeypatch.setattr(discord_mod.subprocess, "run", fake_run)
    return calls


def test_curl_retries_on_5xx(monkeypatch, _no_sleep):
    """A transient 5xx (non-JSON body) is retried; the successful body is returned."""
    calls = _stub_curl_sequence(monkeypatch, [
        _mk_curl_result("<html>Bad Gateway</html>", 502),
        _mk_curl_result('{"id": "abc"}', 200),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    result = client._curl("POST", "/channels/c/messages", {"content": "hi"})
    assert result == {"id": "abc"}
    assert len(calls) == 2


def test_curl_retries_on_non_json_2xx(monkeypatch, _no_sleep):
    """Cloudflare interstitial (200 with HTML body) is retried."""
    calls = _stub_curl_sequence(monkeypatch, [
        _mk_curl_result("<html>error code: 1015</html>", 200),
        _mk_curl_result('{"id": "abc"}', 200),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    result = client._curl("POST", "/channels/c/messages", {"content": "hi"})
    assert result == {"id": "abc"}
    assert len(calls) == 2


def test_curl_retries_on_429_with_retry_after(monkeypatch):
    """A 429 with structured `retry_after` sleeps for that duration, then retries."""
    calls = _stub_curl_sequence(monkeypatch, [
        _mk_curl_result('{"message": "rate limited", "retry_after": 0.25, "code": 20028}', 429),
        _mk_curl_result('{"id": "abc"}', 200),
    ])
    slept: list[float] = []
    monkeypatch.setattr(discord_mod.time, "sleep", lambda t: slept.append(t))
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    result = client._curl("POST", "/channels/c/messages", {"content": "hi"})
    assert result == {"id": "abc"}
    assert slept == [pytest.approx(0.25)]
    assert len(calls) == 2


def test_curl_preserves_edit_rate_limited(monkeypatch, _no_sleep):
    """Discord code 30046 continues to raise `EditRateLimited` (not a retry)."""
    _stub_curl_sequence(monkeypatch, [
        _mk_curl_result('{"code": 30046, "message": "You have reached the maximum edits."}', 429),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    with pytest.raises(EditRateLimited, match="maximum edits"):
        client._curl("PATCH", "/channels/c/messages/1", {"content": "hi"})


def test_curl_raises_with_context_on_persistent_5xx(monkeypatch, _no_sleep):
    """Persistent 5xx exhausts retries and raises a RuntimeError with method/path/status/body."""
    _stub_curl_sequence(monkeypatch, [
        _mk_curl_result("<html>Bad Gateway</html>", 502),
        _mk_curl_result("<html>Bad Gateway</html>", 502),
        _mk_curl_result("<html>Bad Gateway</html>", 502),
        _mk_curl_result("<html>Bad Gateway</html>", 502),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    with pytest.raises(RuntimeError, match=r"POST /channels/c/messages.*HTTP 502.*Bad Gateway"):
        client._curl("POST", "/channels/c/messages", {"content": "hi"})


def test_curl_raises_on_transport_failure(monkeypatch, _no_sleep):
    """Curl transport failure (nonzero exit) surfaces stderr in the RuntimeError."""
    _stub_curl_sequence(monkeypatch, [
        subprocess.CompletedProcess([], returncode=6, stdout="", stderr="Couldn't resolve host"),
        subprocess.CompletedProcess([], returncode=6, stdout="", stderr="Couldn't resolve host"),
        subprocess.CompletedProcess([], returncode=6, stdout="", stderr="Couldn't resolve host"),
        subprocess.CompletedProcess([], returncode=6, stdout="", stderr="Couldn't resolve host"),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    with pytest.raises(RuntimeError, match=r"POST /channels/c/messages.*curl exit 6.*Couldn't resolve host"):
        client._curl("POST", "/channels/c/messages", {"content": "hi"})


def test_curl_returns_none_on_204(monkeypatch, _no_sleep):
    """204 No Content (e.g. DELETE) returns None without retry."""
    calls = _stub_curl_sequence(monkeypatch, [
        _mk_curl_result("", 204),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    result = client._curl("DELETE", "/channels/c/messages/1")
    assert result is None
    assert len(calls) == 1


def test_curl_raises_on_empty_2xx_body(monkeypatch, _no_sleep):
    """A 200 with an empty body when an entity was expected is retried, then raises."""
    _stub_curl_sequence(monkeypatch, [
        _mk_curl_result("", 200),
        _mk_curl_result("", 200),
        _mk_curl_result("", 200),
        _mk_curl_result("", 200),
    ])
    client = DiscordClient(token="x", channel_id="c", guild_id="g")
    with pytest.raises(RuntimeError, match=r"POST /channels/c/messages.*empty body"):
        client._curl("POST", "/channels/c/messages", {"content": "hi"})
