"""Tests for per-message sender overrides (`specs/per-message-sender.md`).

Cover the wire-level payloads emitted by `SlackClient.post()` under the
various resolution paths (message override → client default → unset;
icon_url beats icon_emoji when both resolve), and the Discord-side
one-time warning behavior.
"""
from __future__ import annotations

import warnings

import pytest

from thrds.discord import DiscordClient
from thrds.slack import SlackClient


class _FakeSlackClient(SlackClient):
    """SlackClient stubbing `_request` — records the last payload for assertions."""
    def __init__(self, **kwargs):
        super().__init__(token='x', channel='C_TARGET', **kwargs)
        self.calls: list[tuple[str, dict]] = []

    def _request(self, endpoint, data=None, method='POST'):
        self.calls.append((endpoint, data))
        if endpoint == 'chat.postMessage':
            return {'ts': '1.000001', 'channel': 'C_TARGET'}
        if endpoint == 'chat.update':
            return {'ts': data.get('ts'), 'channel': data.get('channel')}
        raise NotImplementedError(f'no handler for {endpoint}')


def _post_payload(client: _FakeSlackClient) -> dict:
    """Return the last chat.postMessage payload for assertions."""
    endpoint, data = client.calls[-1]
    assert endpoint == 'chat.postMessage'
    return data


# --- resolution: no fields set → payload carries none of them ---

def test_slack_post_no_sender_no_payload_fields():
    """No client defaults + no message overrides → no sender keys in payload at all."""
    client = _FakeSlackClient()
    client.post('hello')
    payload = _post_payload(client)
    assert 'username' not in payload
    assert 'icon_url' not in payload
    assert 'icon_emoji' not in payload


# --- resolution: client defaults ---

def test_slack_post_client_username_default_applied():
    client = _FakeSlackClient(username='Default Sender')
    client.post('hello')
    assert _post_payload(client)['username'] == 'Default Sender'


def test_slack_post_client_icon_url_default_applied():
    client = _FakeSlackClient(icon_url='https://cdn.example/a.png')
    client.post('hello')
    assert _post_payload(client)['icon_url'] == 'https://cdn.example/a.png'


def test_slack_post_client_icon_emoji_default_applied():
    client = _FakeSlackClient(icon_emoji=':robot_face:')
    client.post('hello')
    assert _post_payload(client)['icon_emoji'] == ':robot_face:'


# --- precedence: message-level beats client-level ---

def test_slack_post_msg_username_overrides_client_username():
    client = _FakeSlackClient(username='Default')
    client.post('hello', username='Override')
    assert _post_payload(client)['username'] == 'Override'


def test_slack_post_msg_icon_url_overrides_client_icon_emoji():
    """A message-level `icon_url` fully replaces the client's `icon_emoji` —
    no icon_emoji field in the payload."""
    client = _FakeSlackClient(icon_emoji=':robot_face:')
    client.post('hello', icon_url='https://cdn.example/x.png')
    payload = _post_payload(client)
    assert payload['icon_url'] == 'https://cdn.example/x.png'
    assert 'icon_emoji' not in payload


def test_slack_post_msg_icon_emoji_overrides_client_icon_url():
    """Symmetric: message icon_emoji replaces client icon_url — no icon_url."""
    client = _FakeSlackClient(icon_url='https://cdn.example/default.png')
    client.post('hello', icon_emoji=':new:')
    payload = _post_payload(client)
    assert payload['icon_emoji'] == ':new:'
    assert 'icon_url' not in payload


# --- xor: icon_url beats icon_emoji when BOTH resolve at msg level ---

def test_slack_post_msg_icon_url_wins_over_msg_icon_emoji():
    """Both set at message level → icon_url takes precedence; icon_emoji omitted."""
    client = _FakeSlackClient()
    client.post('hello', icon_url='https://cdn.example/x.png', icon_emoji=':new:')
    payload = _post_payload(client)
    assert payload['icon_url'] == 'https://cdn.example/x.png'
    assert 'icon_emoji' not in payload


def test_slack_post_client_icon_url_wins_over_client_icon_emoji():
    """Same rule at the client-defaults layer — icon_url beats icon_emoji."""
    client = _FakeSlackClient(icon_url='https://cdn.example/default.png', icon_emoji=':robot_face:')
    client.post('hello')
    payload = _post_payload(client)
    assert payload['icon_url'] == 'https://cdn.example/default.png'
    assert 'icon_emoji' not in payload


# --- edit path never carries sender (Slack ignores it on chat.update anyway) ---

def test_slack_edit_payload_has_no_sender_fields():
    """`chat.update` never carries username/icon_* — sender is post-time-only."""
    client = _FakeSlackClient(username='X', icon_url='https://cdn.example/a.png')
    client.edit('1.000001', 'edited')
    endpoint, data = client.calls[-1]
    assert endpoint == 'chat.update'
    assert 'username' not in data
    assert 'icon_url' not in data
    assert 'icon_emoji' not in data


# --- Discord: warn once, ignore ---

class _FakeDiscordClient(DiscordClient):
    """DiscordClient with `_curl` stubbed — no network."""
    def __init__(self):
        super().__init__(token='fake', channel_id='999')
        self.curl_calls: list[tuple[str, str, dict | None]] = []

    def _curl(self, method, path, data=None):
        self.curl_calls.append((method, path, data))
        return {'id': 'm-1'}


def test_discord_post_warns_once_on_sender_override_then_stays_silent():
    """First Msg with sender fields → UserWarning; subsequent ones stay silent."""
    client = _FakeDiscordClient()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        client.post('hello 1', username='Custom')
        client.post('hello 2', icon_url='https://cdn.example/x.png')
        client.post('hello 3', icon_emoji=':wave:')
    # Exactly one warning fires (the first call); subsequent are silenced by the latch.
    sender_warnings = [w for w in caught if 'per-message sender' in str(w.message)]
    assert len(sender_warnings) == 1


def test_discord_post_sender_fields_not_sent_to_api():
    """Ignored means IGNORED: the sender kwargs don't leak into the Discord payload."""
    client = _FakeDiscordClient()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        client.post('hello', username='Custom', icon_url='https://cdn.example/x.png')
    method, path, data = client.curl_calls[-1]
    assert 'username' not in data
    assert 'icon_url' not in data
    assert 'icon_emoji' not in data
    # Content still forwarded.
    assert data['content'] == 'hello'


def test_discord_post_no_warning_when_no_sender_overrides():
    """Bare `post(content)` — no warning, no latch flip."""
    client = _FakeDiscordClient()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        client.post('hello')
        client.post('goodbye')
    sender_warnings = [w for w in caught if 'per-message sender' in str(w.message)]
    assert sender_warnings == []


# --- Slack list_messages populates sender fields for cascade detection ---

class _RepliesClient(SlackClient):
    """SlackClient whose `_request` returns a scripted `conversations.replies`."""
    def __init__(self, messages):
        super().__init__(token='x', channel='C')
        self._messages = messages

    def _request(self, endpoint, data=None, method='POST'):
        if endpoint == 'auth.test':
            return {'user_id': 'UBOT', 'bot_id': 'BBOT'}
        if endpoint == 'conversations.replies':
            return {'messages': self._messages}
        raise NotImplementedError(endpoint)


def test_slack_list_messages_populates_sender_username_and_icon_url():
    """A msg posted with an override comes back with `username` + `icons.image_*`;
    both surface on the `Message` for `_sender_mismatch` to consume."""
    client = _RepliesClient([
        {'ts': '1.000', 'text': 'op', 'user': 'UBOT',
         'username': 'GCS usage — 2026-08-10',
         'icons': {'image_original': 'https://cdn.example/avatar.png',
                   'image_48': 'https://cdn.example/avatar48.png'}},
    ])
    msgs = client.list_messages('1.000')
    assert msgs[0].sender_username == 'GCS usage — 2026-08-10'
    assert msgs[0].sender_icon_url == 'https://cdn.example/avatar.png'
    assert msgs[0].sender_icon_emoji is None   # Slack doesn't return the emoji form


def test_slack_list_messages_falls_back_to_any_image_size_when_original_absent():
    """`icons.image_original` absent → pick any `image_*` (Slack sometimes omits original)."""
    client = _RepliesClient([
        {'ts': '1.001', 'text': 'r1', 'user': 'UBOT',
         'icons': {'image_48': 'https://cdn.example/only-48.png'}},
    ])
    assert client.list_messages('1.000')[0].sender_icon_url == 'https://cdn.example/only-48.png'


def test_slack_list_messages_leaves_sender_fields_none_when_no_override():
    """No `username` / no `icons` on the msg → both sender fields stay None."""
    client = _RepliesClient([{'ts': '1.002', 'text': 'plain', 'user': 'UBOT'}])
    msg = client.list_messages('1.000')[0]
    assert msg.sender_username is None
    assert msg.sender_icon_url is None


# --- Slack get_reactions ---

class _ReactionsClient(SlackClient):
    def __init__(self, response):
        super().__init__(token='x', channel='C')
        self._response = response
        self.calls: list[tuple[str, dict | None, str]] = []

    def _request(self, endpoint, data=None, method='POST'):
        self.calls.append((endpoint, data, method))
        if endpoint == 'reactions.get':
            return self._response
        raise NotImplementedError(endpoint)


def test_slack_get_reactions_returns_reactions_list():
    client = _ReactionsClient({'ok': True, 'message': {'reactions': [
        {'name': 'thumbsup', 'count': 3, 'users': ['U1', 'U2', 'U3']},
        {'name': 'eyes', 'count': 1, 'users': ['U4']},
    ]}})
    result = client.get_reactions('1.000001')
    assert [r['name'] for r in result] == ['thumbsup', 'eyes']
    endpoint, data, method = client.calls[0]
    assert endpoint == 'reactions.get'
    assert data == {'channel': 'C', 'timestamp': '1.000001'}
    assert method == 'GET'


def test_slack_get_reactions_returns_empty_list_when_none():
    """`reactions.get` on a msg with no reactions omits the field — return []."""
    client = _ReactionsClient({'ok': True, 'message': {'text': 'plain'}})
    assert client.get_reactions('1.000001') == []
