"""Tests for staging-only chrome (`StagingChrome`, context blocks).

The load-bearing property: chrome renders as *blocks* while the body stays the
message's `text`. `pull` reads `text`, so chrome cannot round-trip into doc
content — there is no strip step that could fail open and publish a secret-gist
URL. Slack also only unfurls links in `text`, so no chrome link can unfurl.
See `specs/per-thread-model.md`.
"""
from __future__ import annotations

import pytest

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget
from thrds.slack import SLACK_SECTION_LIMIT, SlackClient
from thrds.state import StagingChrome


# --- StagingChrome ---


def test_chrome_defaults_to_every_link_enabled():
    c = StagingChrome()
    assert (c.gist_link, c.target_link, c.posted_link, c.style) == (
        True, True, True, 'context_block',
    )


def test_chrome_rejects_other_styles():
    with pytest.raises(ValueError) as e:
        StagingChrome(style='inline')
    assert str(e.value) == (
        "Unsupported staging-chrome style 'inline'; only 'context_block' "
        "keeps chrome structurally separate from body text"
    )


@pytest.mark.parametrize('gist,target,posted,enabled', [
    (True, True, True, True),
    (True, False, False, True),
    (False, True, False, True),
    (False, False, True, True),
    (False, False, False, False),
])
def test_chrome_any_enabled(gist, target, posted, enabled):
    chrome = StagingChrome(gist_link=gist, target_link=target, posted_link=posted)
    assert chrome.any_enabled is enabled


def test_chrome_round_trips_through_state(tmp_path):
    state = SessionState.new(
        session_slug='s', staging_chrome=StagingChrome(gist_link=False),
    )
    state.save(tmp_path)
    assert SessionState.load(tmp_path).staging_chrome == StagingChrome(gist_link=False)


def test_chrome_coerced_from_dict():
    state = SessionState.new(session_slug='s', staging_chrome={'target_link': False})
    assert state.staging_chrome == StagingChrome(target_link=False)


# --- _chrome_blocks: (header, footer) ---


def _state(**kw) -> SessionState:
    base = dict(session_slug='s', gist_id='abc123', threads={'a': ThreadEntry()})
    base.update(kw)
    return SessionState.new(**base)


def _texts(blocks: list[dict]) -> list[str]:
    return [b['elements'][0]['text'] for b in blocks]


def _posted(**kw) -> SessionState:
    """A session whose only thread went out, with a permalink on record."""
    return _state(threads={'a': ThreadEntry(
        target=ThreadTarget(channel='C0A'),
        state='posted',
        posted_ts='9.9',
        posted_url='https://ex.slack.com/archives/C0A/p99',
    )}, **kw)


def test_blocks_put_gist_link_in_the_footer():
    """Provenance trails the body; the label is the *file* basename."""
    head, foot = SlackClient._chrome_blocks(_state(), 'a', '01-a.md')
    assert (_texts(head), _texts(foot)) == (
        [], ['⤴ <https://gist.github.com/abc123|01-a.md>'],
    )


def test_blocks_put_target_in_the_header():
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0ALERTS'))})
    head, foot = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert (_texts(head), _texts(foot)) == (
        ['→ <#C0ALERTS>'], ['⤴ <https://gist.github.com/abc123|01-a.md>'],
    )


def test_blocks_render_reply_target_with_ts():
    state = _state(threads={
        'a': ThreadEntry(target=ThreadTarget(channel='C0A', thread_ts='1786980761.357209')),
    })
    head, _ = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert _texts(head) == ['→ <#C0A> (reply to `1786980761.357209`)']


def test_blocks_pair_posted_permalink_with_the_target():
    """Where it's going and where it landed read together, in that order."""
    head, _ = SlackClient._chrome_blocks(_posted(), 'a', '01-a.md')
    assert _texts(head) == [
        '→ <#C0A>  ·  ✓ <https://ex.slack.com/archives/C0A/p99|posted>'
    ]


def test_blocks_omit_posted_link_before_a_thread_is_posted():
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0A'))})
    head, _ = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert _texts(head) == ['→ <#C0A>']


def test_blocks_omit_posted_link_when_no_url_was_recorded():
    """`adopt -V` records a ts but no permalink; chrome degrades, doesn't guess."""
    state = _state(threads={
        'a': ThreadEntry(target=ThreadTarget(channel='C0A'), state='posted', posted_ts='9.9'),
    })
    head, _ = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert _texts(head) == ['→ <#C0A>']


def test_blocks_omit_posted_link_when_disabled():
    head, _ = SlackClient._chrome_blocks(
        _posted(staging_chrome=StagingChrome(posted_link=False)), 'a', '01-a.md',
    )
    assert _texts(head) == ['→ <#C0A>']


def test_blocks_use_context_block_type():
    head, foot = SlackClient._chrome_blocks(_posted(), 'a', '01-a.md')
    assert [b['type'] for b in head + foot] == ['context', 'context']


def test_blocks_omit_gist_link_when_disabled():
    state = _state(staging_chrome=StagingChrome(gist_link=False),
                   threads={'a': ThreadEntry(target=ThreadTarget(channel='C0A'))})
    head, foot = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert (_texts(head), foot) == (['→ <#C0A>'], [])


def test_blocks_omit_gist_link_for_no_gist_session():
    state = _state(gist_id=None, threads={'a': ThreadEntry(target=ThreadTarget(channel='C0A'))})
    head, foot = SlackClient._chrome_blocks(state, 'a', '01-a.md')
    assert (_texts(head), foot) == (['→ <#C0A>'], [])


def test_blocks_omit_header_when_target_unresolvable():
    head, foot = SlackClient._chrome_blocks(_state(), 'a', '01-a.md')
    assert (head, _texts(foot)) == ([], ['⤴ <https://gist.github.com/abc123|01-a.md>'])


def test_blocks_empty_when_nothing_resolvable():
    assert SlackClient._chrome_blocks(_state(gist_id=None), 'a', '01-a.md') == ([], [])


def test_blocks_empty_when_all_disabled():
    state = _state(staging_chrome=StagingChrome(
        gist_link=False, target_link=False, posted_link=False,
    ))
    assert SlackClient._chrome_blocks(state, 'a', '01-a.md') == ([], [])


# --- _chrome_for_threads / _register_chrome: slug → blocks → OP content ---


def _client() -> SlackClient:
    return SlackClient(token='xoxp-fake', channel='C0S')


def _thread(slug: str, *contents: str) -> DocThread:
    return DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)


def test_chrome_keyed_by_slug():
    client = _client()
    state = _state(threads={'a': ThreadEntry(), 'b': ThreadEntry()})
    threads = [_thread('a', 'A op.'), _thread('b', 'B op.')]
    got = client._chrome_for_threads(threads, state, {'a': '01-a.md', 'b': '02-b.md'})
    assert sorted(got) == ['a', 'b']


def test_chrome_empty_when_disabled():
    client = _client()
    state = _state(staging_chrome=StagingChrome(
        gist_link=False, target_link=False, posted_link=False,
    ))
    assert client._chrome_for_threads([_thread('a', 'A.')], state, {}) == {}


def test_register_binds_only_the_op_content():
    client = _client()
    threads = [_thread('a', 'OP body.', 'A reply.')]
    client._chrome_by_slug = client._chrome_for_threads(threads, _state(), {'a': '01-a.md'})
    client._register_chrome(threads)
    assert list(client._chrome_by_content) == ['OP body.']


def test_register_binds_every_content_variant_of_one_op():
    """Authored text, placeholder-URL text, and real-permalink text are the
    same thread — all three must resolve to its chrome, or a cross-ref-carrying
    OP loses chrome the moment refs resolve."""
    client = _client()
    authored = [_thread('a', 'See [x](#b).')]
    resolved = [_thread('a', 'See <https://ex.slack.com/p1|x>.')]
    client._chrome_by_slug = client._chrome_for_threads(authored, _state(), {'a': '01-a.md'})
    client._register_chrome(authored)
    client._register_chrome(resolved)
    assert sorted(client._chrome_by_content) == [
        'See <https://ex.slack.com/p1|x>.', 'See [x](#b).',
    ]
    assert (
        client._chrome_by_content['See [x](#b).']
        == client._chrome_by_content['See <https://ex.slack.com/p1|x>.']
    )


def test_register_skips_thread_with_no_messages_of_ours():
    client = _client()
    foreign = DocThread(messages=[DocMessage(content='Theirs.', author='rafal')], slug='a')
    client._chrome_by_slug = client._chrome_for_threads([foreign], _state(), {})
    client._register_chrome([foreign])
    assert client._chrome_by_content == {}


# --- _attach_chrome: the text/blocks split ---


def _ctx(text: str) -> dict:
    return {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': text}]}


def test_attach_leaves_text_as_body_alone():
    """The property that makes chrome unable to leak (or unfurl): `text` is body-only."""
    client = _client()
    client._chrome_by_content = {'Body.': ([_ctx('→ x')], [_ctx('⤴ y')])}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['text'] == 'Body.'


def test_attach_wraps_the_body_section_in_header_and_footer():
    client = _client()
    head, foot = _ctx('→ <#C0A>'), _ctx('⤴ gist')
    client._chrome_by_content = {'Body.': ([head], [foot])}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['blocks'] == [
        head,
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
        foot,
    ]


def test_attach_handles_a_footer_only_thread():
    client = _client()
    foot = _ctx('⤴ gist')
    client._chrome_by_content = {'Body.': ([], [foot])}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['blocks'] == [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
        foot,
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
    client._chrome_by_content = {'OP.': ([], [_ctx('⤴ y')])}
    data = {'text': 'A reply.'}
    client._attach_chrome(data, 'A reply.', 'A reply.')
    assert data == {'text': 'A reply.'}


def test_attach_skipped_when_body_exceeds_section_limit():
    """A correct body matters more than the affordance — post it without chrome
    rather than splitting mid-mrkdwn."""
    client = _client()
    long_body = 'x' * (SLACK_SECTION_LIMIT + 1)
    client._chrome_by_content = {long_body: ([], [_ctx('⤴ y')])}
    data = {'text': long_body}
    client._attach_chrome(data, long_body, long_body)
    assert 'blocks' not in data


def test_attach_applies_at_exactly_the_section_limit():
    client = _client()
    body = 'x' * SLACK_SECTION_LIMIT
    client._chrome_by_content = {body: ([], [_ctx('⤴ y')])}
    data = {'text': body}
    client._attach_chrome(data, body, body)
    assert 'blocks' in data


def test_promote_sends_no_blocks(monkeypatch, tmp_path):
    """Prod purity: chrome is attached only by the staging path, so a promoted
    message goes out as plain text with no chrome block."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    sent: list[dict] = []

    def fake_request(endpoint, data=None, **kw):
        sent.append({'method': endpoint, **(data or {})})
        return {'ts': '9.9', 'ok': True, 'permalink': 'https://ex.slack.com/x'}

    monkeypatch.setattr(client, '_request', fake_request)
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0PROD'))})
    client.promote_thread(
        'a', _thread('a', 'Body.'), ThreadTarget(channel='C0PROD'), state, pace=0.0,
    )
    posts = [s for s in sent if s['method'] == 'chat.postMessage']
    assert [p['text'] for p in posts] == ['Body.']
    assert all('blocks' not in p for p in posts)


def test_promote_records_the_permalink(monkeypatch, tmp_path):
    """So later staging pushes can link the draft to what it became, offline."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {
        'ts': '9.9', 'ok': True, 'permalink': 'https://ex.slack.com/archives/C0PROD/p99',
    })
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0PROD'))})
    client.promote_thread(
        'a', _thread('a', 'Body.'), ThreadTarget(channel='C0PROD'), state, pace=0.0,
    )
    entry = state.thread('a')
    assert (entry.posted_ts, entry.posted_url) == (
        '9.9', 'https://ex.slack.com/archives/C0PROD/p99',
    )


def test_staging_chrome_cleared_after_sync(monkeypatch, tmp_path):
    """Cleared in `finally`, so a promote later in the same process can't
    inherit chrome from an earlier staging push."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {'ts': '1.1', 'ok': True})
    state = _state(staging_channel='C0S')
    client.sync_threads_staging(
        [_thread('a', 'Body.')], state, pace=0.0, filenames={'a': '01-a.md'},
    )
    assert client._chrome_by_content is None


# --- _reconcile_chrome: chrome converges even when the body doesn't change ---


def _reconcile_client(monkeypatch, live_blocks: list[dict] | None) -> tuple[SlackClient, list]:
    """A client whose staged OP currently renders ``live_blocks``."""
    client = _client()
    calls: list[dict] = []

    def fake_request(endpoint, data=None, **kw):
        calls.append({'endpoint': endpoint, **(data or {})})
        if endpoint == 'conversations.replies':
            return {'messages': [{'ts': '1.1', 'text': 'Body.', 'blocks': live_blocks}]}
        return {'ok': True, 'ts': '1.1'}

    monkeypatch.setattr(client, '_request', fake_request)
    return client, calls


def test_context_texts_projects_only_context_blocks():
    """Slack echoes blocks back with `block_id`s; the authored text is what's
    comparable."""
    blocks = [
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': '→ <#C0A>'}], 'block_id': 'xY1'},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': '⤴ gist'}], 'block_id': 'zQ2'},
    ]
    assert SlackClient._context_texts(blocks) == ['→ <#C0A>', '⤴ gist']


def test_context_texts_of_a_message_with_no_blocks():
    assert SlackClient._context_texts(None) == []


def test_reconcile_edits_when_live_chrome_is_missing(monkeypatch):
    """The case that matters: a body `core.sync` SKIPped, whose chrome is due."""
    client, calls = _reconcile_client(monkeypatch, None)
    client._chrome_by_content = {'Body.': ([_ctx('→ <#C0A>')], [])}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is True
    assert [c['endpoint'] for c in calls] == ['conversations.replies', 'chat.update']


def test_reconcile_edit_carries_the_chrome(monkeypatch):
    client, calls = _reconcile_client(monkeypatch, None)
    head = _ctx('→ <#C0A>')
    client._chrome_by_content = {'Body.': ([head], [])}
    client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0)
    update = [c for c in calls if c['endpoint'] == 'chat.update'][0]
    assert update['blocks'] == [
        head, {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
    ]


def test_reconcile_noop_when_live_chrome_already_matches(monkeypatch):
    """Idempotent: a second push with nothing changed edits nothing."""
    live = [
        _ctx('→ <#C0A>'),
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Body.'}},
    ]
    client, calls = _reconcile_client(monkeypatch, live)
    client._chrome_by_content = {'Body.': ([_ctx('→ <#C0A>')], [])}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is False
    assert [c['endpoint'] for c in calls] == ['conversations.replies']


def test_reconcile_edits_when_chrome_drifted(monkeypatch):
    """A thread promoted since the last push is now due a `✓ posted` link."""
    live = [_ctx('→ <#C0A>')]
    client, calls = _reconcile_client(monkeypatch, live)
    client._chrome_by_content = {
        'Body.': ([_ctx('→ <#C0A>  ·  ✓ <https://ex.slack.com/p99|posted>')], []),
    }
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is True


def test_reconcile_noop_for_content_with_no_chrome(monkeypatch):
    """Replies aren't registered, so they cost no API call either."""
    client, calls = _reconcile_client(monkeypatch, None)
    client._chrome_by_content = {'OP.': ([_ctx('→ <#C0A>')], [])}
    assert client._reconcile_chrome('1.1', 'A reply.', pace=0.0, jitter=0.0) is False
    assert calls == []


def test_reconcile_noop_when_body_exceeds_section_limit(monkeypatch):
    """Matches `_attach_chrome`'s bail-out, so it can't loop trying to converge
    chrome that will never be attached."""
    client, calls = _reconcile_client(monkeypatch, None)
    long_body = 'x' * (SLACK_SECTION_LIMIT + 1)
    client._chrome_by_content = {long_body: ([_ctx('→ <#C0A>')], [])}
    assert client._reconcile_chrome('1.1', long_body, pace=0.0, jitter=0.0) is False
    assert calls == []


def test_push_applies_newly_due_chrome_to_an_unchanged_body(monkeypatch, tmp_path):
    """End to end: `core.sync` compares content, so without an explicit
    reconcile a promoted thread's `✓ posted` link would never appear until the
    draft text happened to change."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    calls: list[dict] = []

    def fake_request(endpoint, data=None, **kw):
        calls.append({'endpoint': endpoint, **(data or {})})
        if endpoint == 'conversations.replies':
            # Body already in sync; no chrome on it yet.
            return {'messages': [{'ts': '1.1', 'text': 'Body.', 'user': 'U0ME'}]}
        return {'ok': True, 'ts': '1.1'}

    monkeypatch.setattr(client, '_request', fake_request)
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1',
        target=ThreadTarget(channel='C0A'),
        state='posted',
        posted_ts='9.9',
        posted_url='https://ex.slack.com/archives/C0A/p99',
    )})
    client.sync_threads_staging(
        [_thread('a', 'Body.')], state, pace=0.0, filenames={'a': '01-a.md'},
    )
    updates = [c for c in calls if c['endpoint'] == 'chat.update']
    assert [SlackClient._context_texts(u['blocks']) for u in updates] == [[
        '→ <#C0A>  ·  ✓ <https://ex.slack.com/archives/C0A/p99|posted>',
        '⤴ <https://gist.github.com/abc123|01-a.md>',
    ]]


# --- _body_text: Slack strips newlines from `text` when blocks are present ---


def _msg(text: str, blocks: list[dict] | None = None) -> dict:
    m = {'ts': '1.1', 'text': text}
    if blocks is not None:
        m['blocks'] = blocks
    return m


def _section(text: str) -> dict:
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}


def test_body_text_prefers_the_section_over_flattened_text():
    """The regression this exists for: Slack demotes `text` to a one-line
    notification fallback once a message carries blocks, so reading `text`
    would flatten every multi-line staged message on the next pull."""
    body = 'Line one.\n\n1. item\n2. item'
    m = _msg('Line one. 1. item 2. item', [_ctx('→ <#C0A>'), _section(body), _ctx('⤴ g')])
    assert SlackClient._body_text(m) == body


def test_body_text_never_reads_a_context_block():
    """Chrome still can't round-trip into a doc — only sections are read."""
    m = _msg('', [_ctx('→ <#C0A>'), _ctx('⤴ gist')])
    assert SlackClient._body_text(m) == ''


def test_body_text_falls_back_to_text_without_blocks():
    assert SlackClient._body_text(_msg('Plain\nbody.')) == 'Plain\nbody.'


def test_body_text_falls_back_for_a_foreign_multi_section_message():
    """Several sections isn't a shape we author; joining them would be a guess."""
    m = _msg('fallback', [_section('a'), _section('b')])
    assert SlackClient._body_text(m) == 'fallback'


def test_body_text_ignores_an_empty_section():
    m = _msg('fallback', [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': ''}}])
    assert SlackClient._body_text(m) == 'fallback'


def test_pull_reads_the_section_so_multiline_bodies_survive(monkeypatch):
    """End to end through `list_messages`, the path a pull actually takes."""
    body = 'Para one.\n\nPara two.'
    client = _client()
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {'messages': [
        _msg('Para one. Para two.', [_section(body), _ctx('⤴ g')]) | {'user': 'U0ME'},
    ]})
    assert [m.content for m in client.list_messages('1.1')] == [body]
