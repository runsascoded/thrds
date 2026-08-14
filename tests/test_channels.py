"""Tests for `SlackClient.list_channels_by_name` — the channel-name lookup
backing the CLI's `#name` / bare-name resolution across every channel-taking
command.

Mirrors `test_recover.py`'s `_FakeSlackClient(handler)` pattern.
"""
from __future__ import annotations

import pytest

from thrds.slack import SlackClient


class _FakeSlackClient(SlackClient):
    """SlackClient with `_request` stubbed to a scripted `conversations.list` handler."""
    def __init__(self, handler):
        super().__init__(token='x', channel='')
        self._handler = handler
        self.calls: list[tuple[str, dict, str]] = []

    def _request(self, endpoint, data=None, method='POST'):
        self.calls.append((endpoint, data, method))
        if endpoint == 'conversations.list':
            return self._handler(data)
        raise NotImplementedError(f'no handler for {endpoint}')


def _single_page(channels):
    def handler(data):
        return {'channels': channels, 'response_metadata': {}}
    return handler


# --- request shape (guards against regressions of live-caught bugs) ---

def test_list_channels_uses_GET_not_JSON_post():
    """`conversations.list` must be GET/urlencoded — a JSON POST silently
    returns only public channels (Slack quietly ignores the `types` param).
    This test locks in the fix.
    """
    client = _FakeSlackClient(_single_page([]))
    client.list_channels_by_name()
    assert len(client.calls) == 1
    endpoint, data, method = client.calls[0]
    assert endpoint == 'conversations.list'
    assert method == 'GET'


def test_list_channels_requests_public_private_and_mpim_types():
    """`types` param must include private_channel so we can resolve
    the staging PCs `thrds slack init` creates."""
    client = _FakeSlackClient(_single_page([]))
    client.list_channels_by_name()
    _, data, _ = client.calls[0]
    types = set(data['types'].split(','))
    assert types == {'public_channel', 'private_channel', 'mpim'}


def test_list_channels_includes_archived():
    """`exclude_archived='false'` (STRING) — archived staging PCs are a
    real recovery target; string not bool because form-encoding."""
    client = _FakeSlackClient(_single_page([]))
    client.list_channels_by_name()
    _, data, _ = client.calls[0]
    assert data['exclude_archived'] == 'false'


# --- return shape ---

def test_list_channels_returns_name_to_id_map():
    client = _FakeSlackClient(_single_page([
        {'id': 'C001', 'name': 'general'},
        {'id': 'C002', 'name': 'random'},
        {'id': 'G003', 'name': 'private-thing'},
    ]))
    assert client.list_channels_by_name() == {
        'general': 'C001',
        'random': 'C002',
        'private-thing': 'G003',
    }


def test_list_channels_skips_channels_missing_name_or_id():
    """Defensive: Slack payload edge cases (missing fields) → skip that row,
    don't crash the whole scan."""
    client = _FakeSlackClient(_single_page([
        {'id': 'C001', 'name': 'general'},
        {'id': 'C002'},                    # no name
        {'name': 'no-id'},                 # no id
        {},                                # empty
        {'id': 'C003', 'name': 'valid'},
    ]))
    assert client.list_channels_by_name() == {'general': 'C001', 'valid': 'C003'}


# --- pagination ---

def test_list_channels_follows_next_cursor():
    pages = [
        {'channels': [{'id': 'C001', 'name': 'a'}, {'id': 'C002', 'name': 'b'}],
         'response_metadata': {'next_cursor': 'AFTER_1'}},
        {'channels': [{'id': 'C003', 'name': 'c'}],
         'response_metadata': {'next_cursor': ''}},
    ]
    idx = [0]

    def handler(data):
        page = pages[idx[0]]
        idx[0] += 1
        return page

    client = _FakeSlackClient(handler)
    assert client.list_channels_by_name() == {'a': 'C001', 'b': 'C002', 'c': 'C003'}
    # Second page carries the cursor.
    assert client.calls[0][1].get('cursor') is None
    assert client.calls[1][1]['cursor'] == 'AFTER_1'


# --- caching ---

def test_list_channels_result_is_cached_per_instance():
    """Second call returns the same dict without hitting the API again."""
    calls_seen = [0]

    def handler(data):
        calls_seen[0] += 1
        return {'channels': [{'id': 'C001', 'name': 'general'}], 'response_metadata': {}}

    client = _FakeSlackClient(handler)
    first = client.list_channels_by_name()
    second = client.list_channels_by_name()
    assert first == second == {'general': 'C001'}
    assert calls_seen[0] == 1
