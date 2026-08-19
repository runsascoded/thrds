"""Tests for `push` / `pull` on a per-thread session.

Staging keeps terraform semantics (files are the desired state); `--prod` is
gone from `push` because promoting is per-thread and deliberate. `pull --write`
rewrites each thread's own file, which is what makes a Slack-side edit — or a
deletion — land as a version of *that message* rather than of a shared doc.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.doc import DocSyncResult


@dataclass
class SyncSpy:
    """SlackClient stand-in recording the per-thread sync/pull calls."""
    token: str = ''
    channel: str = ''
    staging_calls: list[dict] = field(default_factory=list)
    pull_calls: list[str] = field(default_factory=list)
    pull_returns: list[DocThread] = field(default_factory=list)

    def __init__(self, *, token, channel):
        self.token = token
        self.channel = channel
        self.staging_calls = []
        self.pull_calls = []
        self.pull_returns = []

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def sync_threads_staging(self, threads, state, dry_run=False, **kw):
        self.staging_calls.append({
            'slugs': [t.slug for t in threads],
            'messages': {t.slug: [m.content for m in t.messages] for t in threads},
            'dry_run': dry_run,
        })
        return DocSyncResult(
            channel='C0STAGE',
            preamble_ts=None,
            thread_ts_by_slug={t.slug: f'{i + 1}.0' for i, t in enumerate(threads)},
            thread_results={},
            deleted_slugs=[],
        )

    def pull_threads_staging(self, state, session_dir=None):
        self.pull_calls.append(state.staging_channel)
        return self.pull_returns

    def pull_chrome_edits(self, state, filenames=None):
        return {}

    def adopt_new_staging_threads(self, state, session_dir):
        return []


@pytest.fixture
def spy(monkeypatch):
    the_spy = SyncSpy(token='xoxp-fake', channel='')
    monkeypatch.setattr('thrds.cli.SlackClient', lambda *, token, channel: the_spy)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return the_spy


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n\n+++\n\nAlpha reply.\n')
    (tmp_path / '02-beta.md').write_text('Beta OP.\n')
    SessionState.new(
        session_slug='s',
        staging_channel='C0STAGE',
        threads={'alpha': ThreadEntry(staging_ts='1.1'), 'beta': ThreadEntry(staging_ts='2.2')},
    ).save(tmp_path)
    return tmp_path


def _run(*args):
    return CliRunner().invoke(cli, ['slack', *args], catch_exceptions=False)


# --- push ---


def test_push_sends_all_thread_files_in_file_order(session, spy):
    result = _run('push')
    assert result.exit_code == 0, result.output
    assert [c['slugs'] for c in spy.staging_calls] == [['alpha', 'beta']]


def test_push_sends_each_threads_messages(session, spy):
    _run('push')
    assert spy.staging_calls[0]['messages'] == {
        'alpha': ['Alpha OP.', 'Alpha reply.'],
        'beta': ['Beta OP.'],
    }


def test_push_dry_run_flag_propagates(session, spy):
    _run('push', '-n')
    assert [c['dry_run'] for c in spy.staging_calls] == [True]


def test_push_prod_rejected_on_per_thread_session(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'push', '-p'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Per-thread sessions push only to staging; use '
        '`thrds slack promote <slug>` to post a thread to its target.'
    )
    assert spy.staging_calls == []


def test_push_channel_flag_rejected_on_per_thread_session(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'push', '-c', '#foo'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Per-thread sessions push only to staging; use '
        '`thrds slack promote <slug>` to post a thread to its target.'
    )


def test_push_errors_with_no_thread_files(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    SessionState.new(session_slug='s', staging_channel='C0S').save(tmp_path)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: No thread files (`NN-slug.md`) in this session.'
    )


# --- pull ---


def _threads(*specs):
    return [
        DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)
        for slug, contents in specs
    ]


def test_pull_rewrites_each_thread_file(session, spy):
    spy.pull_returns = _threads(
        ('alpha', ['Alpha OP edited in Slack.', 'Alpha reply.']),
        ('beta', ['Beta OP.']),
    )
    result = _run('pull')
    assert result.exit_code == 0, result.output
    assert (session / '01-alpha.md').read_text() == (
        'Alpha OP edited in Slack.\n\n+++\n\nAlpha reply.\n'
    )


def test_pull_captures_a_deleted_reply(session, spy):
    """Deleting a reply in Slack must land as a version of that file — that's
    how a draft gets retracted without losing the record that it existed."""
    spy.pull_returns = _threads(('alpha', ['Alpha OP.']), ('beta', ['Beta OP.']))
    _run('pull')
    assert (session / '01-alpha.md').read_text() == 'Alpha OP.\n'


def test_pull_reports_files_written(session, spy):
    spy.pull_returns = _threads(('alpha', ['A.']), ('beta', ['B.']))
    result = _run('pull')
    assert result.stderr.rstrip().split('\n')[-1] == (
        'wrote 2 thread file(s): 01-alpha.md, 02-beta.md'
    )


def test_pull_dry_run_leaves_files_untouched(session, spy):
    spy.pull_returns = _threads(('alpha', ['Changed.']), ('beta', ['B.']))
    before = (session / '01-alpha.md').read_text()
    _run('pull', '-n')
    assert (session / '01-alpha.md').read_text() == before


def test_pull_dry_run_prints_each_thread_to_stdout(session, spy):
    spy.pull_returns = _threads(('alpha', ['A body.']), ('beta', ['B body.']))
    result = _run('pull', '-n')
    assert result.stdout == 'A body.\nB body.\n'


def test_pull_skips_threads_absent_from_slack(session, spy):
    """A file with no staging counterpart is left alone rather than emptied."""
    spy.pull_returns = _threads(('alpha', ['Only alpha came back.']))
    before = (session / '02-beta.md').read_text()
    _run('pull')
    assert (session / '02-beta.md').read_text() == before


def test_pull_prod_rejected_on_per_thread_session(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'pull', '-p'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Per-thread sessions pull from staging only; a promoted thread '
        'is tracked by its `posted_ts`.'
    )
