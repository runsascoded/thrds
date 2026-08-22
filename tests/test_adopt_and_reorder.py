"""Tests for the two things that let you drive a session from inside Slack:

- adopting a thread written straight into the staging channel
- renaming / renumbering thread files (from a chrome edit, or `reorder`)
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from thrds import SessionState, ThreadEntry, ThreadTarget
from thrds.chrome import Chrome
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.slack import SlackClient
from thrds.threadfile import dedupe_thread_filename, slugify, thread_files


# --- slugify ---


@pytest.mark.parametrize('text,expected', [
    ('Latest MFU (still 6%, but chip-wide now)', 'latest-mfu-still-6-but-chip'),
    ('**MFU** update\n\nbody', 'mfu-update'),
    ('`neuronx-cc` segfault in `hlo2penguin`', 'neuronx-cc-segfault-in-hlo2penguin'),
    ('<https://ex.com|Profiling> / Improving MFU', 'profiling-improving-mfu'),
    ('   ', ''),
    ('!!! ???', ''),
])
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_slugify_reads_only_the_first_line():
    """A draft's first line is its subject far more often than anything else."""
    assert slugify('Title here\n\nA much longer second paragraph.') == 'title-here'


# --- dedupe_thread_filename ---


def test_dedupe_returns_the_plain_name_when_free():
    assert dedupe_thread_filename(4, 'idea', set()) == '04-idea.md'


def test_dedupe_suffixes_the_slug_not_the_index():
    """The number means "where this sorts"; bumping it to dodge a collision
    would quietly make it mean "how many retries"."""
    assert dedupe_thread_filename(4, 'idea', {'04-idea.md'}) == '04-idea-2.md'


def test_dedupe_keeps_walking():
    taken = {'04-idea.md', '04-idea-2.md', '04-idea-3.md'}
    assert dedupe_thread_filename(4, 'idea', taken) == '04-idea-4.md'


# --- _name_for_adopted ---


def _name(chrome, body='Some draft body.', index=3, taken=frozenset()):
    return SlackClient._name_for_adopted(chrome, body, index, set(taken))


def test_adopted_name_prefers_an_explicit_filename():
    """That's the author saying what to call it and where to sort it."""
    assert _name(Chrome(channel='C0T', filename='07-mine.md')) == '07-mine.md'


def test_adopted_name_falls_back_to_the_opening_words():
    assert _name(Chrome(channel='C0T'), body='Latest MFU update') == '03-latest-mfu-update.md'


def test_adopted_name_falls_back_again_for_an_unsluggable_body():
    assert _name(Chrome(channel='C0T'), body='!!!') == '03-untitled.md'


def test_adopted_name_takes_a_bare_name_at_the_next_index():
    """`cuda-graph.md` says what it's called, not where it sorts."""
    assert _name(Chrome(channel='C0T', filename='cuda-graph.md')) == '03-cuda-graph.md'


def test_adopted_name_ignores_a_filename_it_cannot_parse():
    assert _name(Chrome(channel='C0T', filename='not a slug!.md'), body='Hi') == '03-hi.md'


# --- adopt_new_staging_threads ---


def _adopt_client(monkeypatch, history):
    client = SlackClient(token='xoxp-fake', channel='C0S')
    monkeypatch.setattr(SlackClient, 'bot_ids', property(lambda self: ('U0ME', None)))

    def fake_request(endpoint, data=None, **kw):
        if endpoint == 'conversations.history':
            return {'messages': history}
        if endpoint == 'conversations.replies':
            ts = data['ts']
            return {'messages': [m for m in history if m['ts'] == ts]}
        return {'ok': True}

    monkeypatch.setattr(client, '_request', fake_request)
    return client


def _msg(ts, text, user='U0ME', **kw):
    return {'ts': ts, 'text': text, 'user': user, **kw}


def test_adopt_takes_a_message_with_a_chrome_line(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'A new draft.\n\n→ <#C0T>')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [(a.slug, a.filename) for a in adopted] == [('a-new-draft', '01-a-new-draft.md')]


def test_adopt_records_the_target(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Draft.\n\n→ <#C0T>')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    client.adopt_new_staging_threads(state, tmp_path)
    assert state.threads['draft'].target == ThreadTarget(channel='C0T')


def test_adopt_records_the_staging_ts(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Draft.\n\n→ <#C0T>')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    client.adopt_new_staging_threads(state, tmp_path)
    assert state.threads['draft'].staging_ts == '5.5'


def test_adopt_strips_the_chrome_from_the_body(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Draft body.\n\n→ <#C0T>')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [m.content for m in adopted[0].thread.messages] == ['Draft body.']


def test_adopt_skips_a_message_with_no_chrome(tmp_path, monkeypatch):
    """What separates "a new draft" from "a note to self in the scratchpad"."""
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Just thinking out loud.')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    assert client.adopt_new_staging_threads(state, tmp_path) == []


def test_adopt_skips_someone_elses_message(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Draft.\n\n→ <#C0T>', user='U0THEM')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    assert client.adopt_new_staging_threads(state, tmp_path) == []


def test_adopt_skips_channel_join_and_archive_events(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [
        _msg('5.5', 'archived the channel\n\n→ <#C0T>', subtype='channel_archive'),
    ])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    assert client.adopt_new_staging_threads(state, tmp_path) == []


def test_adopt_skips_a_thread_already_known(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [_msg('5.5', 'Draft.\n\n→ <#C0T>')])
    state = SessionState.new(
        session_slug='s', staging_channel='C0S',
        threads={'draft': ThreadEntry(staging_ts='5.5')},
    )
    assert client.adopt_new_staging_threads(state, tmp_path) == []


def test_adopt_numbers_several_drafts_in_the_order_written(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [
        _msg('7.7', 'Third one.\n\n→ <#C0T>'),
        _msg('5.5', 'First one.\n\n→ <#C0T>'),
        _msg('6.6', 'Second one.\n\n→ <#C0T>'),
    ])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [a.filename for a in adopted] == [
        '01-first-one.md', '02-second-one.md', '03-third-one.md',
    ]


def test_adopt_starts_after_existing_files(tmp_path, monkeypatch):
    """Adopting never renumbers a thread that's already there."""
    (tmp_path / '01-alpha.md').write_text('A.\n')
    (tmp_path / '05-beta.md').write_text('B.\n')
    client = _adopt_client(monkeypatch, [_msg('5.5', 'New one.\n\n→ <#C0T>')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [a.filename for a in adopted] == ['06-new-one.md']


def test_adopt_honors_an_explicit_filename(tmp_path, monkeypatch):
    client = _adopt_client(monkeypatch, [
        _msg('5.5', 'Body.\n\n→ <#C0T> · 04-my-name.md'),
    ])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [(a.slug, a.filename) for a in adopted] == [('my-name', '04-my-name.md')]


def test_adopt_takes_chrome_on_the_first_line(tmp_path, monkeypatch):
    """Writing a new draft, leading with where it's going is natural."""
    client = _adopt_client(monkeypatch, [_msg('5.5', '→ <#C0T>\n\nThe draft body.')])
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    adopted = client.adopt_new_staging_threads(state, tmp_path)
    assert [m.content for m in adopted[0].thread.messages] == ['The draft body.']


def test_adopt_takes_target_and_name_from_one_line(tmp_path, monkeypatch):
    client = _adopt_client(
        monkeypatch,
        [_msg('5.5', 'Body.\n\n→ <#C0T> · 02-named.md')],
    )
    state = SessionState.new(session_slug='s', staging_channel='C0S')
    client.adopt_new_staging_threads(state, tmp_path)
    assert state.threads['named'].target == ThreadTarget(channel='C0T')


# --- reorder ---


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    for name in ('01-alpha.md', '03-beta.md', '07-gamma.md'):
        (tmp_path / name).write_text(f'{name} body.\n')
    SessionState.new(session_slug='s', staging_channel='C0S', threads={
        'alpha': ThreadEntry(staging_ts='1.1'),
        'beta': ThreadEntry(staging_ts='2.2'),
        'gamma': ThreadEntry(staging_ts='3.3'),
    }).save(tmp_path)
    return tmp_path


def _run(*args):
    return CliRunner().invoke(cli, ['slack', *args], catch_exceptions=False)


def test_reorder_compacts_gaps(session):
    _run('reorder')
    assert [f.name for f in thread_files(session)] == [
        '01-alpha.md', '02-beta.md', '03-gamma.md',
    ]


def test_reorder_preserves_each_file_body(session):
    _run('reorder')
    assert (session / '03-gamma.md').read_text() == '07-gamma.md body.\n'


def test_reorder_puts_named_slugs_first(session):
    _run('reorder', 'gamma', 'alpha')
    assert [f.name for f in thread_files(session)] == [
        '01-gamma.md', '02-alpha.md', '03-beta.md',
    ]


def test_reorder_handles_a_swap(session):
    """Two-phase rename: 01→02 straight through would clobber what's at 02."""
    _run('reorder')
    _run('reorder', 'beta', 'alpha')
    assert [f.name for f in thread_files(session)] == [
        '01-beta.md', '02-alpha.md', '03-gamma.md',
    ]


def test_reorder_leaves_state_untouched(session):
    """`thrds.yml` is keyed by slug, so staging pointers follow their thread."""
    _run('reorder', 'gamma')
    assert {s: e.staging_ts for s, e in SessionState.load(session).threads.items()} == {
        'alpha': '1.1', 'beta': '2.2', 'gamma': '3.3',
    }


def test_reorder_reports_each_rename(session):
    result = _run('reorder')
    assert result.stderr.rstrip().split('\n') == [
        '  03-beta.md → 02-beta.md',
        '  07-gamma.md → 03-gamma.md',
        'renumbered 2 file(s)',
    ]


def test_reorder_noop_when_already_gapless(session):
    _run('reorder')
    result = _run('reorder')
    assert result.stderr.rstrip() == 'Already gapless and in order; nothing to do.'


def test_reorder_dry_run_renames_nothing(session):
    _run('reorder', '-n')
    assert [f.name for f in thread_files(session)] == [
        '01-alpha.md', '03-beta.md', '07-gamma.md',
    ]


def test_reorder_rejects_an_unknown_slug(session):
    result = CliRunner().invoke(cli, ['slack', 'reorder', 'nope'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: No thread(s) nope in this session; available: alpha, beta, gamma'
    )


# --- chrome written into a thread file ---


class _ChromeSpy:
    """Enough SlackClient for `_absorb_file_chrome` and the push path."""

    def __init__(self, *, token, channel):
        self.channel = channel
        self.synced = None

    def list_channels_by_name(self):
        return {'marin-alerts': 'C0NAMED'}

    def _resolve_chrome_channel(self, chrome):
        if chrome.channel is not None:
            return chrome.channel
        if chrome.channel_name is None:
            return None
        return self.list_channels_by_name().get(chrome.channel_name)

    def sync_threads_staging(self, threads, state, dry_run=False, filenames=None):
        from thrds.doc import DocSyncResult
        self.synced = threads
        return DocSyncResult(
            channel='C0S', preamble_ts=None,
            thread_ts_by_slug={t.slug: '1.1' for t in threads},
            thread_results={}, deleted_slugs=[],
        )


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    spy = _ChromeSpy(token='x', channel='')
    monkeypatch.setattr('thrds.cli.SlackClient', lambda **kw: spy)
    SessionState.new(session_slug='s', staging_channel='C0S').save(tmp_path)
    return tmp_path, spy


def test_file_chrome_sets_the_target(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('→ <#C0T>\n\nThe draft body.\n')
    _run('push')
    assert SessionState.load(session).thread('seed').target == ThreadTarget(channel='C0T')


def test_file_chrome_is_not_posted_as_body(seeded):
    """Left alone it would go out as the message's first line — wrong, and
    hard to notice."""
    session, spy = seeded
    (session / '01-seed.md').write_text('→ <#C0T>\n\nThe draft body.\n')
    _run('push')
    assert [m.content for m in spy.synced[0].messages] == ['The draft body.']


def test_file_chrome_is_removed_from_the_file(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('→ <#C0T>\n\nThe draft body.\n')
    _run('push')
    assert (session / '01-seed.md').read_text() == 'The draft body.\n'


def test_file_chrome_accepts_a_pasted_thread_link(seeded):
    """The parent from `thread_ts=`, not the reply's own ts in the path."""
    session, spy = seeded
    url = ('https://openathena.slack.com/archives/C0BN20081CH/p1787087651317099'
           '?thread_ts=1786980761.357209&cid=C0BN20081CH')
    (session / '01-seed.md').write_text(f'→ {url}\n\nBody.\n')
    _run('push')
    assert SessionState.load(session).thread('seed').target == ThreadTarget(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_file_chrome_resolves_a_channel_name(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('→ #marin-alerts\n\nBody.\n')
    _run('push')
    assert SessionState.load(session).thread('seed').target == ThreadTarget(
        channel='C0NAMED',
    )


def test_file_chrome_reports_what_it_did(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('→ <#C0T>\n\nBody.\n')
    result = _run('push')
    assert result.stderr.split('\n')[0] == (
        'targeted seed → C0T (from a chrome line in its file)'
    )


def test_a_file_with_no_chrome_is_untouched(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('Just a body.\n')
    _run('push')
    assert (session / '01-seed.md').read_text() == 'Just a body.\n'


def test_file_chrome_dry_run_leaves_the_file_alone(seeded):
    session, spy = seeded
    (session / '01-seed.md').write_text('→ <#C0T>\n\nBody.\n')
    _run('push', '-n')
    assert (session / '01-seed.md').read_text() == '→ <#C0T>\n\nBody.\n'
