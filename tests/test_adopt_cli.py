"""Tests for `thrds slack adopt` — record an already-posted prod message.

For threads that went out before thrds tracked them (posted by hand, or by a
version that never recorded `prod_threads`). Posts nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from thrds import SessionState, ThreadEntry, ThreadTarget
from thrds.cli import SLACK_TOKEN_ENV, cli


@dataclass
class AdoptSpy:
    token: str = ''
    channel: str = ''
    channels_by_name: dict[str, str] = field(default_factory=dict)
    permalink_calls: list[tuple[str, str]] = field(default_factory=list)
    permalink_raises: Exception | None = None
    post_calls: list[dict] = field(default_factory=list)

    def __init__(self, *, token, channel):
        self.token = token
        self.channel = channel
        self.channels_by_name = {}
        self.permalink_calls = []
        self.permalink_raises = None
        self.post_calls = []

    def list_channels_by_name(self) -> dict[str, str]:
        return dict(self.channels_by_name)

    def permalink(self, ts: str) -> str:
        if self.permalink_raises is not None:
            raise self.permalink_raises
        self.permalink_calls.append((self.channel, ts))
        return f'https://ex.slack.com/archives/{self.channel}/p{ts.replace(".", "")}'

    def post(self, *a, **kw):
        self.post_calls.append({'args': a})
        raise AssertionError('adopt must not post')


@pytest.fixture
def spy(monkeypatch):
    the_spy = AdoptSpy(token='xoxp-fake', channel='')

    def factory(*, token, channel):
        the_spy.token = token
        the_spy.channel = channel
        return the_spy

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return the_spy


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n')
    SessionState.new(
        session_slug='s', staging_channel='C0S', threads={'alpha': ThreadEntry(staging_ts='1.1')},
    ).save(tmp_path)
    return tmp_path


def _run(*args):
    return CliRunner().invoke(cli, ['slack', *args], catch_exceptions=False)


def test_adopt_marks_thread_posted(session, spy):
    result = _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert result.exit_code == 0, result.output
    entry = SessionState.load(session).threads['alpha']
    assert (entry.state, entry.posted_ts, entry.target) == (
        'posted', '9.9', ThreadTarget(channel='C0PROD'),
    )


def test_adopt_preserves_staging_ts(session, spy):
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert SessionState.load(session).threads['alpha'].staging_ts == '1.1'


def test_adopt_posts_nothing(session, spy):
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert spy.post_calls == []


def test_adopt_verifies_ts_by_permalink(session, spy):
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert spy.permalink_calls == [('C0PROD', '9.9')]


def test_adopt_reports_permalink_and_result(session, spy):
    result = _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert result.stderr.rstrip().split('\n') == [
        '  https://ex.slack.com/archives/C0PROD/p99',
        'adopted alpha → C0PROD @ 9.9',
    ]


def test_adopt_resolves_channel_name(session, spy):
    spy.channels_by_name['oa-amazon-trainium'] = 'C0RESOLVED'
    _run('adopt', 'alpha', '-c', '#oa-amazon-trainium', '-t', '9.9')
    assert SessionState.load(session).threads['alpha'].target == ThreadTarget(channel='C0RESOLVED')


def test_adopt_errors_on_unresolvable_ts(session, spy):
    spy.permalink_raises = RuntimeError('message_not_found')
    result = CliRunner().invoke(cli, ['slack', 'adopt', 'alpha', '-c', 'C0P', '-t', 'bogus'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Could not resolve bogus in C0P: message_not_found'
    )


def test_adopt_leaves_state_unchanged_on_verify_failure(session, spy):
    spy.permalink_raises = RuntimeError('message_not_found')
    CliRunner().invoke(cli, ['slack', 'adopt', 'alpha', '-c', 'C0P', '-t', 'bogus'])
    assert SessionState.load(session).threads['alpha'].state == 'draft'


def test_adopt_records_the_permalink_it_verified(session, spy):
    """The verify call already holds the URL; keeping it saves every later
    reader (staging chrome, `status`) an API round-trip."""
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert SessionState.load(session).threads['alpha'].posted_url == (
        'https://ex.slack.com/archives/C0PROD/p99'
    )


def test_adopt_no_verify_records_no_permalink(session, spy):
    """No check, no URL — chrome degrades rather than fabricating a link."""
    spy.permalink_raises = RuntimeError('should not be called')
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9', '-V')
    assert SessionState.load(session).threads['alpha'].posted_url is None


def test_adopt_no_verify_skips_permalink(session, spy):
    spy.permalink_raises = RuntimeError('should not be called')
    result = _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9', '-V')
    assert result.exit_code == 0
    assert SessionState.load(session).threads['alpha'].state == 'posted'


def test_adopt_unknown_slug_lists_available(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'adopt', 'nope', '-c', 'C0P', '-t', '9.9'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No thread 'nope' in this session; available: alpha"
    )


def test_adopt_warns_when_overwriting_a_posted_thread(session, spy):
    state = SessionState.load(session)
    state.thread('alpha').state = 'posted'
    state.thread('alpha').posted_ts = '1.0'
    state.save(session)
    result = _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert result.stderr.split('\n')[0] == (
        'note: alpha was already posted (ts 1.0); overwriting'
    )


def test_adopt_satisfies_the_archive_gate(session, spy):
    """Adopting every thread should let `archive` proceed."""
    _run('adopt', 'alpha', '-c', 'C0PROD', '-t', '9.9')
    assert SessionState.load(session).pending_threads() == []


def test_adopt_rejects_legacy_session(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    SessionState.new(doc_path='d.md', staging_threads={'a': '1.1'}).save(tmp_path)
    result = CliRunner().invoke(cli, ['slack', 'adopt', 'a', '-c', 'C0P', '-t', '9.9'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: This session is still on the legacy single-doc layout; '
        'run `thrds slack migrate` first.'
    )
