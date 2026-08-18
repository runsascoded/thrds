"""Tests for staging chrome as it moves through `SlackClient`.

Rendering, attaching, converging on drift, stripping on pull, and refusing to
promote a body that still carries a footer.
"""
from __future__ import annotations

import pytest

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget
from thrds.slack import SLACK_MESSAGE_LIMIT, SlackClient
from thrds.state import StagingChrome

PARENT = 'https://openathena.slack.com/archives/C0BN20081CH/p1786980761357209'
POSTED = 'https://openathena.slack.com/archives/C0P/p99'


def _state(**kw) -> SessionState:
    base = dict(session_slug='s', gist_id='abc123', threads={'a': ThreadEntry()})
    base.update(kw)
    return SessionState.new(**base)


def _client() -> SlackClient:
    return SlackClient(token='xoxp-fake', channel='C0S')


def _thread(slug: str, *contents: str) -> DocThread:
    return DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)


# --- StagingChrome ---


def test_chrome_defaults():
    c = StagingChrome()
    assert (c.gist_link, c.target_link, c.posted_link, c.style) == (
        True, True, True, 'footer',
    )


def test_chrome_coerces_the_retired_block_style():
    """It names a version, not a choice — refusing to load would strand every
    session written by the block-based version behind a hand-edit."""
    assert StagingChrome(style='context_block').style == 'footer'


def test_chrome_rejects_an_unknown_style():
    with pytest.raises(ValueError) as e:
        StagingChrome(style='inline')
    assert str(e.value) == (
        "Unsupported staging-chrome style 'inline'; only 'footer' "
        "keeps staged messages editable in Slack"
    )


@pytest.mark.parametrize('gist,target,posted,enabled', [
    (True, True, True, True),
    (False, True, False, True),
    (False, False, True, True),
    (False, False, False, False),
])
def test_chrome_any_enabled(gist, target, posted, enabled):
    assert StagingChrome(
        gist_link=gist, target_link=target, posted_link=posted,
    ).any_enabled is enabled


# --- _chrome_line ---


def test_line_for_a_posted_top_level_thread():
    state = _state(threads={'a': ThreadEntry(
        target=ThreadTarget(channel='C0T'), state='posted',
        posted_ts='9.9', posted_url=POSTED,
    )})
    assert SlackClient._chrome_line(state, 'a', '01-a.md') == (
        f'→ <#C0T> · <{POSTED}|posted> · '
        f'<https://gist.github.com/abc123#file-01-a-md|01-a.md>'
    )


def test_line_for_an_untargeted_draft_is_the_gist_alone():
    assert SlackClient._chrome_line(_state(), 'a', '01-a.md') == (
        '<https://gist.github.com/abc123#file-01-a-md|01-a.md>'
    )


def test_line_links_the_arrow_for_a_reply_target():
    state = _state(threads={'a': ThreadEntry(
        target=ThreadTarget(channel='C0T', thread_ts='1786980761.357209'),
    )}, gist_id=None)
    assert SlackClient._chrome_line(state, 'a', '01-a.md', PARENT) == f'<{PARENT}|→> (<#C0T>)'


def test_line_omits_disabled_affordances():
    state = _state(
        staging_chrome=StagingChrome(gist_link=False, posted_link=False),
        threads={'a': ThreadEntry(target=ThreadTarget(channel='C0T'), posted_url=POSTED)},
    )
    assert SlackClient._chrome_line(state, 'a', '01-a.md') == '→ <#C0T>'


def test_line_none_when_nothing_resolvable():
    assert SlackClient._chrome_line(_state(gist_id=None), 'a', '01-a.md') is None


# --- _attach_chrome ---


def test_attach_appends_the_footer_to_the_text():
    client = _client()
    client._chrome_by_content = {'Body.': '→ <#C0T>'}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['text'] == 'Body.\n\n→ <#C0T>'


def test_attach_clears_blocks_so_the_message_stays_editable():
    """A message left carrying blocks from the previous chrome design would
    still have no Edit affordance in Slack."""
    client = _client()
    client._chrome_by_content = {'Body.': '→ <#C0T>'}
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data['blocks'] == []


def test_attach_clears_blocks_for_every_message_in_a_staging_sync():
    """Even one getting no footer — `chat.update` leaves stale blocks alone
    unless told otherwise, and those keep a message uneditable."""
    client = _client()
    client._chrome_by_content = {'OP.': '→ <#C0T>'}
    data = {'text': 'A reply.'}
    client._attach_chrome(data, 'A reply.', 'A reply.')
    assert data == {'text': 'A reply.', 'blocks': []}


def test_attach_sends_no_blocks_key_outside_a_staging_sync():
    """Promote and the CRUD verbs shouldn't carry chrome bookkeeping."""
    client = _client()
    client._chrome_by_content = None
    data = {'text': 'Body.'}
    client._attach_chrome(data, 'Body.', 'Body.')
    assert data == {'text': 'Body.'}


def test_attach_uses_the_wire_text_not_the_markdown():
    client = _client()
    client._chrome_by_content = {'**md**': '→ <#C0T>'}
    data = {'text': '*wire*'}
    client._attach_chrome(data, '**md**', '*wire*')
    assert data['text'] == '*wire*\n\n→ <#C0T>'


def test_attach_noop_for_content_without_chrome():
    """Replies aren't registered; only OPs get a footer."""
    client = _client()
    client._chrome_by_content = {'OP.': '→ <#C0T>'}
    data = {'text': 'A reply.'}
    client._attach_chrome(data, 'A reply.', 'A reply.')
    assert data['text'] == 'A reply.'


def test_attach_skipped_when_the_footer_would_overflow():
    """A complete body matters more than the affordance."""
    client = _client()
    body = 'x' * (SLACK_MESSAGE_LIMIT - 3)
    client._chrome_by_content = {body: '→ <#C0T>'}
    data = {'text': body}
    client._attach_chrome(data, body, body)
    assert data['text'] == body


def test_attach_applies_when_the_footer_just_fits():
    client = _client()
    footer = '→ <#C0T>'
    body = 'x' * (SLACK_MESSAGE_LIMIT - len(footer) - 2)
    client._chrome_by_content = {body: footer}
    data = {'text': body}
    client._attach_chrome(data, body, body)
    assert data['text'] == f'{body}\n\n{footer}'


# --- _register_chrome: content variants ---


def test_register_binds_only_the_op_content():
    client = _client()
    threads = [_thread('a', 'OP body.', 'A reply.')]
    client._chrome_by_slug = {'a': '→ <#C0T>'}
    client._register_chrome(threads)
    assert client._chrome_by_content == {'OP body.': '→ <#C0T>'}


def test_register_binds_every_content_variant_of_one_op():
    """Authored text, placeholder-URL text and real-permalink text are all the
    same thread, so all three resolve to its footer — otherwise a
    cross-ref-carrying OP loses chrome the moment refs resolve."""
    client = _client()
    client._chrome_by_slug = {'a': '→ <#C0T>'}
    client._register_chrome([_thread('a', 'See [x](#b).')])
    client._register_chrome([_thread('a', 'See <https://ex/p1|x>.')])
    assert sorted(client._chrome_by_content) == [
        'See <https://ex/p1|x>.', 'See [x](#b).',
    ]


# --- pull: the footer is stripped before it can reach a doc ---


def test_pull_strips_the_footer(monkeypatch):
    body = 'Para one.\n\nPara two.'
    client = _client()
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {'messages': [
        {'ts': '1.1', 'user': 'U0ME', 'text': f'{body}\n\n→ <#C0T>'},
    ]})
    assert [m.content for m in client.list_messages('1.1')] == [body]


def test_pull_keeps_a_body_that_has_no_footer(monkeypatch):
    client = _client()
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))
    monkeypatch.setattr(client, '_request', lambda *a, **kw: {'messages': [
        {'ts': '1.1', 'user': 'U0ME', 'text': 'One.\n\nTwo.'},
    ]})
    assert [m.content for m in client.list_messages('1.1')] == ['One.\n\nTwo.']


# --- promote: fail closed ---


def _promote_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _client()
    sent: list[dict] = []

    def fake_request(endpoint, data=None, **kw):
        sent.append({'endpoint': endpoint, **(data or {})})
        return {'ok': True, 'ts': '9.9', 'permalink': POSTED, 'messages': []}

    monkeypatch.setattr(client, '_request', fake_request)
    return client, sent


def test_promote_refuses_a_body_that_still_carries_a_footer(monkeypatch, tmp_path):
    """Stripping on pull is a step that can fail open; publishing a secret-gist
    URL to a real channel is worth a check at the boundary."""
    client, sent = _promote_client(monkeypatch, tmp_path)
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0P'))})
    thread = _thread('a', 'Body.\n\n→ <#C0T>')
    with pytest.raises(ValueError) as e:
        client.promote_thread('a', thread, ThreadTarget(channel='C0P'), state, pace=0.0)
    assert str(e.value) == (
        "Thread 'a': message(s) at index [0] still carry a staging chrome footer; "
        "refusing to post. Re-pull the thread (or delete the trailing footer line) "
        "before promoting."
    )


def test_promote_posts_nothing_when_it_refuses(monkeypatch, tmp_path):
    client, sent = _promote_client(monkeypatch, tmp_path)
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0P'))})
    with pytest.raises(ValueError):
        client.promote_thread(
            'a', _thread('a', 'Body.\n\n→ <#C0T>'),
            ThreadTarget(channel='C0P'), state, pace=0.0,
        )
    assert [s['endpoint'] for s in sent if s['endpoint'] == 'chat.postMessage'] == []


def test_promote_sends_a_clean_body_with_no_chrome(monkeypatch, tmp_path):
    client, sent = _promote_client(monkeypatch, tmp_path)
    state = _state(threads={'a': ThreadEntry(target=ThreadTarget(channel='C0P'))})
    client.promote_thread(
        'a', _thread('a', 'Body.'), ThreadTarget(channel='C0P'), state, pace=0.0,
    )
    posts = [s for s in sent if s['endpoint'] == 'chat.postMessage']
    assert [p['text'] for p in posts] == ['Body.']


# --- _reconcile_chrome: converges even when the body doesn't change ---


def _reconcile_client(monkeypatch, live_text: str):
    client = _client()
    calls: list[dict] = []

    def fake_request(endpoint, data=None, **kw):
        calls.append({'endpoint': endpoint, **(data or {})})
        if endpoint == 'conversations.replies':
            return {'messages': [{'ts': '1.1', 'text': live_text}]}
        return {'ok': True, 'ts': '1.1'}

    monkeypatch.setattr(client, '_request', fake_request)
    return client, calls


def test_reconcile_edits_when_the_footer_is_missing(monkeypatch):
    """The case that matters: a body `core.sync` SKIPped, whose footer is due."""
    client, calls = _reconcile_client(monkeypatch, 'Body.')
    client._chrome_by_content = {'Body.': '→ <#C0T>'}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is True
    assert [c['endpoint'] for c in calls] == ['conversations.replies', 'chat.update']


def test_reconcile_noop_when_the_footer_already_matches(monkeypatch):
    client, calls = _reconcile_client(monkeypatch, 'Body.\n\n→ <#C0T>')
    client._chrome_by_content = {'Body.': '→ <#C0T>'}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is False
    assert [c['endpoint'] for c in calls] == ['conversations.replies']


def test_reconcile_edits_when_the_footer_drifted(monkeypatch):
    """A thread promoted since the last push is now due a `posted` link."""
    client, calls = _reconcile_client(monkeypatch, 'Body.\n\n→ <#C0T>')
    client._chrome_by_content = {'Body.': f'→ <#C0T> · <{POSTED}|posted>'}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is True


def test_reconcile_noop_for_content_with_no_chrome(monkeypatch):
    client, calls = _reconcile_client(monkeypatch, 'A reply.')
    client._chrome_by_content = {'OP.': '→ <#C0T>'}
    assert client._reconcile_chrome('1.1', 'A reply.', pace=0.0, jitter=0.0) is False
    assert calls == []


# --- pull_chrome_edits: retarget by editing the footer in Slack ---


def _edits_client(monkeypatch, live_text: str):
    client = _client()
    monkeypatch.setattr(client, '_request', lambda endpoint, data=None, **kw: (
        {'messages': [{'ts': '1.1', 'text': live_text}]}
    ))
    return client


def test_edits_retarget_to_a_different_channel(monkeypatch):
    client = _edits_client(monkeypatch, 'Body.\n\n→ <#C0NEW>')
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1', target=ThreadTarget(channel='C0OLD'),
    )})
    edits = client.pull_chrome_edits(state)
    assert state.thread('a').target == ThreadTarget(channel='C0NEW')
    assert edits['a'].target_now == ThreadTarget(channel='C0NEW')


def test_edits_retarget_into_a_thread_from_a_pasted_link(monkeypatch):
    client = _edits_client(monkeypatch, f'Body.\n\n→ <{PARENT}>')
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(staging_ts='1.1')})
    client.pull_chrome_edits(state)
    assert state.thread('a').target == ThreadTarget(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_edits_report_nothing_when_the_footer_is_unchanged(monkeypatch):
    client = _edits_client(monkeypatch, 'Body.\n\n→ <#C0T>')
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1', target=ThreadTarget(channel='C0T'),
    )})
    assert client.pull_chrome_edits(state) == {}


def test_edits_skip_terminal_threads(monkeypatch):
    """A posted thread's target records where it went; the footer can't move it."""
    client = _edits_client(monkeypatch, 'Body.\n\n→ <#C0NEW>')
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1', target=ThreadTarget(channel='C0OLD'),
        state='posted', posted_ts='9.9',
    )})
    assert client.pull_chrome_edits(state) == {}
    assert state.thread('a').target == ThreadTarget(channel='C0OLD')


def test_edits_report_a_renamed_file_without_acting_on_it(monkeypatch):
    """Renaming is how you reorder threads, but it breaks the per-file git
    history the layout exists for — a human's call, not a pull's."""
    client = _edits_client(
        monkeypatch,
        'Body.\n\n→ <#C0T> · <https://gist.github.com/abc123#file-03-a-md|03-a.md>',
    )
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1', target=ThreadTarget(channel='C0T'),
    )})
    edits = client.pull_chrome_edits(state, {'a': '01-a.md'})
    assert (edits['a'].renamed_to, edits['a'].target_now) == ('03-a.md', None)


def test_edits_ignore_a_matching_filename(monkeypatch):
    client = _edits_client(
        monkeypatch,
        'Body.\n\n→ <#C0T> · <https://gist.github.com/abc123#file-01-a-md|01-a.md>',
    )
    state = _state(staging_channel='C0S', threads={'a': ThreadEntry(
        staging_ts='1.1', target=ThreadTarget(channel='C0T'),
    )})
    assert client.pull_chrome_edits(state, {'a': '01-a.md'}) == {}


def test_reconcile_noop_against_slack_entity_encoding(monkeypatch):
    """Slack HTML-encodes `&` on storage, so a permalink comes back with
    `&amp;cid=`. Comparing that raw would report drift on every push and
    re-edit every message forever."""
    url = 'https://ex.slack.com/archives/C0P/p99?thread_ts=1.1&cid=C0P'
    footer = f'→ <#C0T> · <{url}|posted>'
    live = footer.replace('&', '&amp;')
    client, calls = _reconcile_client(monkeypatch, f'Body.\n\n{live}')
    client._chrome_by_content = {'Body.': footer}
    assert client._reconcile_chrome('1.1', 'Body.', pace=0.0, jitter=0.0) is False
