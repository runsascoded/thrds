"""Tests for `_resolve_custom_emoji`'s ``download`` switch.

Read-only verbs (`fetch`, `diff`, the push/promote gates) observe Slack via
``download_emoji=False``: the substituted text must be byte-identical to what
`pull` would write — the emoji filename is deterministic given the workspace
URL — while nothing lands in the session dir. Otherwise either "fetch touches
nothing" or "a fetch right after a pull is a nop" would be a lie.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from thrds import Doc, DocMessage, DocThread, SessionState
from thrds.slack import SlackClient


def _doc(content: str) -> Doc:
    return Doc(threads=[DocThread(slug='alpha', messages=[DocMessage(content=content)])])


@pytest.fixture
def client(monkeypatch):
    c = SlackClient(token='xoxp-fake', channel='C0')
    c.download_calls = []
    monkeypatch.setattr(
        c, 'fetch_workspace_emoji',
        lambda: {'party-blob': 'https://emoji.example/party-blob.png'},
    )

    def download(url: str, dest: Path) -> None:
        c.download_calls.append((url, dest.name))
        dest.write_bytes(b'PNG')

    monkeypatch.setattr(c, '_download_url', download)
    return c


def _state() -> SessionState:
    return SessionState.new(session_slug='s', staging_channel='C0STAGE')


def test_download_mode_writes_the_file_and_substitutes(client, tmp_path):
    state = _state()
    doc = client._resolve_custom_emoji(_doc('Hi :party-blob:!'), state, tmp_path)
    assert doc.threads[0].messages[0].content == (
        'Hi ![:party-blob:](emoji-party-blob.png)!'
    )
    assert client.download_calls == [
        ('https://emoji.example/party-blob.png', 'emoji-party-blob.png'),
    ]
    assert (tmp_path / 'emoji-party-blob.png').read_bytes() == b'PNG'
    assert state.workspace_emoji == {'party-blob': 'emoji-party-blob.png'}


def test_observation_mode_substitutes_identically_without_writing(client, tmp_path):
    """The property `remotes.observe` relies on: same text, no files."""
    state = _state()
    doc = client._resolve_custom_emoji(
        _doc('Hi :party-blob:!'), state, tmp_path, download=False,
    )
    assert doc.threads[0].messages[0].content == (
        'Hi ![:party-blob:](emoji-party-blob.png)!'
    )
    assert client.download_calls == []
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert state.workspace_emoji == {'party-blob': 'emoji-party-blob.png'}


def test_unknown_name_stays_literal_in_both_modes(client, tmp_path):
    for download in (True, False):
        doc = client._resolve_custom_emoji(
            _doc('So :not-a-workspace-emoji:.'), _state(), tmp_path, download=download,
        )
        assert doc.threads[0].messages[0].content == 'So :not-a-workspace-emoji:.'
    assert client.download_calls == []


def test_cached_name_skips_the_workspace_fetch(client, monkeypatch, tmp_path):
    def boom():
        raise AssertionError('emoji.list should not be fetched for cached names')

    monkeypatch.setattr(client, 'fetch_workspace_emoji', boom)
    state = _state()
    state.workspace_emoji = {'party-blob': 'emoji-party-blob.png'}
    doc = client._resolve_custom_emoji(
        _doc(':party-blob:'), state, tmp_path, download=False,
    )
    assert doc.threads[0].messages[0].content == (
        '![:party-blob:](emoji-party-blob.png)'
    )
