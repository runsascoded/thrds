"""Tests for staging-only chrome (`StagingChrome`, context blocks).

The load-bearing property: chrome renders as a *block* while the body stays the
message's `text`. `pull` reads `text`, so chrome cannot round-trip into doc
content — there is no strip step that could fail open and publish a secret-gist
URL. See `specs/per-thread-model.md`.
"""
from __future__ import annotations

import pytest

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget
from thrds.slack import SLACK_SECTION_LIMIT, SlackClient
from thrds.state import StagingChrome


# --- StagingChrome ---


def test_chrome_defaults_to_both_links_enabled():
    c = StagingChrome()
    assert (c.gist_link, c.target_link, c.style) == (True, True, 'context_block')


def test_chrome_rejects_other_styles():
    with pytest.raises(ValueError) as e:
        StagingChrome(style='inline')
    assert str(e.value) == (
        "Unsupported staging-chrome style 'inline'; only 'context_block' "
        "keeps chrome structurally separate from body text"
    )


@pytest.mark.parametrize('gist,target,enabled', [
    (True, True, True),
    (True, False, True),
    (False, True, True),
    (False, False, False),
])
def test_chrome_any_enabled(gist, target, enabled):
    assert StagingChrome(gist_link=gist, target_link=target).any_enabled is enabled


def test_chrome_round_trips_through_state(tmp_path):
    state = SessionState.new(
        session_slug='s', staging_chrome=StagingChrome(gist_link=False),
    )
    state.save(tmp_path)
    assert SessionState.load(tmp_path).staging_chrome == StagingChrome(gist_link=False)


def test_chrome_coerced_from_dict():
    state = SessionState.new(session_slug='s', staging_chrome={'target_link': False})
    assert state.staging_chrome == StagingChrome(target_link=False)


# --- _chrome_elements ---


def _state(**kw) -> SessionState:
    base = dict(session_slug='s', gist_id='abc123', threads={'a': ThreadEntry()})
    base.update(kw)
    return SessionState.new(**base)


def _text(elements: list[dict]) -> str:
    return elements[0]['elements'][0]['text']


def test_elements_render_gist_link_with_file_basename():
    """The label is the *file* basename — meaningful now that thread ↔ file."""
    els = SlackClient._chrome_elements(_state(), 'a', '01-a.md')
    assert _text(els) == '⤴ <https://gist.github.com/abc123|01-a.md>'


def test_elements_render_target_channel():
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0ALERTS'))})
    assert _text(SlackClient._chrome_elements(state, 'a', '01-a.md')) == (
        '⤴ <https://gist.github.com/abc123|01-a.md>  ·  → <#C0ALERTS>'
    )


def test_elements_render_reply_target_with_ts():
    state = _state(threads={
        'a': ThreadEntry(target=ThreadTarget(channel='C0A', thread_ts='1786980761.357209')),
    })
    assert _text(SlackClient._chrome_elements(state, 'a', '01-a.md')) == (
        '⤴ <https://gist.github.com/abc123|01-a.md>  ·  '
        '→ <#C0A> (reply to `1786980761.357209`)'
    )


def test_elements_use_context_block_type():
    els = SlackClient._chrome_elements(_state(), 'a', '01-a.md')
    assert els[0]['type'] == 'context'


def test_elements_omit_gist_link_when_disabled():
    state = _state(staging_chrome=StagingChrome(gist_link=False),
                   threads={'a': ThreadEntry(target=ThreadTarget(channel='C0A'))})
    assert _text(SlackClient._chrome_elements(state, 'a', '01-a.md')) == '→ <#C0A>'


def test_elements_omit_gist_link_for_no_gist_session():
    state = _state(gist_id=None, threads={'a': ThreadEntry(target=ThreadTarget(channel='C0A'))})
    assert _text(SlackClient._chrome_elements(state, 'a', '01-a.md')) == '→ <#C0A>'


def test_elements_omit_target_when_unresolvable():
    assert _text(SlackClient._chrome_elements(_state(), 'a', '01-a.md')) == (
        '⤴ <https://gist.github.com/abc123|01-a.md>'
    )


def test_elements_empty_when_nothing_resolvable():
    state = _state(gist_id=None)
    assert SlackClient._chrome_elements(state, 'a', '01-a.md') == []


def test_elements_empty_when_all_disabled():
    state = _state(staging_chrome=StagingChrome(gist_link=False, target_link=False))
    assert SlackClient._chrome_elements(state, 'a', '01-a.md') == []


# --- _chrome_for_threads: OPs only ---


def _client() -> SlackClient:
    return SlackClient(token='xoxp-fake', channel='C0S')


def _thread(slug: str, *contents: str) -> DocThread:
    return DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)


def test_chrome_maps_only_the_op_content():
    client = _client()
    threads = [_thread('a', 'OP body.', 'A reply.')]
    got = client._chrome_for_threads(threads, _state(), {'a': '01-a.md'})
    assert list(got) == ['OP body.']


def test_chrome_covers_every_thread():
    client = _client()
    state = _state(threads={'a': ThreadEntry(), 'b': ThreadEntry()})
    threads = [_thread('a', 'A op.'), _thread('b', 'B op.')]
    got = client._chrome_for_threads(threads, state, {'a': '01-a.md', 'b': '02-b.md'})
    assert sorted(got) == ['A op.', 'B op.']


def test_chrome_empty_when_disabled():
    client = _client()
    state = _state(staging_chrome=StagingChrome(gist_link=False, target_link=False))
    assert client._chrome_for_threads([_thread('a', 'A.')], state, {}) == {}


def test_chrome_skips_thread_with_no_messages_of_ours():
    client = _client()
    foreign = DocThread(messages=[DocMessage(content='Theirs.', author='rafal')], slug='a')
    assert client._chrome_for_threads([foreign], _state(), {}) == {}


# --- _attach_chrome: the text/blocks split ---


def test_attach_leaves_text_as_body_alone():
    """The property that makes chrome unable to leak: `text` is body-only."""
    client = _client()
    client._chrome_by_content = {'Body.': [{'type': 'context', 'elements': []}]}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['text'] == 'Body.'


def test_attach_puts_body_in_a_section_and_chrome_after_it():
    client = _client()
    chrome = [{'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': '⤴ x'}]}]
    client._chrome_by_content = {'Body.': chrome}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['blocks'] == [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
        chrome[0],
    ]


def test_attach_noop_when_no_chrome_registered():
    client = _client()
    client._chrome_by_content = None
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data == {'text': 'Body.'}


def test_attach_noop_for_content_without_chrome():
    """Replies get no chrome — only the OP is registered."""
    client = _client()
    client._chrome_by_content = {'OP.': [{'type': 'context', 'elements': []}]}
    data = {'text': 'A reply.'}
    client._attach_chrome(data, 'A reply.', 'A reply.')
    assert data == {'text': 'A reply.'}


def test_attach_skipped_when_body_exceeds_section_limit():
    """A correct body matters more than the affordance — post it without chrome
    rather than splitting mid-mrkdwn."""
    client = _client()
    long_body = 'x' * (SLACK_SECTION_LIMIT + 1)
    client._chrome_by_content = {long_body: [{'type': 'context', 'elements': []}]}
    data = {'text': long_body}
    client._attach_chrome(data, long_body, long_body)
    assert 'blocks' not in data


def test_attach_applies_at_exactly_the_section_limit():
    client = _client()
    body = 'x' * SLACK_SECTION_LIMIT
    client._chrome_by_content = {body: [{'type': 'context', 'elements': []}]}
    data = {'text': body}
    client._attach_chrome(data, body, body)
    assert 'blocks' in data


def test_promote_sends_no_blocks(monkeypatch):
    """Prod purity: chrome is attached only by the staging path, so a promoted
    message goes out as plain text with no chrome block."""
    client = _client()
    sent: list[dict] = []

    def fake_request(method, data=None, **kw):
        sent.append({'method': method, **(data or {})})
        return {'ts': '9.9', 'ok': True}

    monkeypatch.setattr(client, '_request', fake_request)
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0PROD'))})
    client.promote_thread(
        'a', _thread('a', 'Body.'), ThreadTarget(channel='C0PROD'), state, pace=0.0,
    )
    posts = [s for s in sent if s['method'] == 'chat.postMessage']
    assert [p['text'] for p in posts] == ['Body.']
    assert all('blocks' not in p for p in posts)


def test_staging_chrome_cleared_after_sync(monkeypatch):
    """Cleared in `finally`, so a promote later in the same process can't
    inherit chrome from an earlier staging push."""
    client = _client()
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {'ts': '1.1', 'ok': True})
    state = _state(staging_channel='C0S')
    client.sync_threads_staging(
        [_thread('a', 'Body.')], state, pace=0.0, filenames={'a': '01-a.md'},
    )
    assert client._chrome_by_content is None


def test_attach_uses_wire_text_in_the_section():
    """The section carries the converted mrkdwn, matching what `text` carries."""
    client = _client()
    client._chrome_by_content = {'*md*': [{'type': 'context', 'elements': []}]}
    data = {'text': '*wire*'}
    client._attach_chrome(data, '*md*', '*wire*')
    assert data['blocks'][0]['text']['text'] == '*wire*'
