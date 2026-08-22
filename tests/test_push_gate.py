"""Tests for `slck push`'s upstream gate, commit-first shape, and verification.

Slack has no non-fast-forward rejection, so `push` supplies one: refuse to
overwrite staging state that moved since our last fetch. And git pushes
commits, not working trees, so `push` commits first and sends exactly that —
then records what Slack *observed*ly holds, not what we intended it to.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, tracking
from thrds.doc import DocSyncResult
from thrds.cli import SLACK_TOKEN_ENV, cli


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@dataclass
class PushSpy:
    """Slack stand-in: `returns` is the channel's current state; a (non-dry)
    sync overwrites it with what was pushed, like the real channel would."""
    returns: dict[str, DocThread] = field(default_factory=dict)
    prod_returns: dict[str, DocThread] = field(default_factory=dict)
    synced: list[list[str]] = field(default_factory=list)
    normalize: dict[str, str] = field(default_factory=dict)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.prod_returns = {}
        self.synced = []
        self.normalize = {}

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def pull_thread_states(self, remote, state, session_dir=None, slugs=None, download_emoji=True):
        returns = self.returns if remote.name == 'staging' else self.prod_returns
        return list(returns.values())

    def pull_chrome_edits(self, state, filenames=None):
        return {}

    def adopt_new_staging_threads(self, state, session_dir):
        return []

    def sync_threads_staging(self, threads, state, dry_run=False, filenames=None, **kw):
        self.synced.append([t.slug for t in threads])
        if not dry_run:
            self.returns = {
                t.slug: DocThread(slug=t.slug, messages=[
                    DocMessage(content=self.normalize.get(m.content, m.content))
                    for m in t.messages
                ])
                for t in threads
            }
        return DocSyncResult(
            channel='C0STAGE',
            preamble_ts=None,
            thread_ts_by_slug={t.slug: f'{i}.0' for i, t in enumerate(threads, 1)},
            thread_results={},
            deleted_slugs=[],
        )


def thread(slug: str, *contents: str) -> DocThread:
    return DocThread(slug=slug, messages=[DocMessage(content=c) for c in contents])


@pytest.fixture
def spy(monkeypatch):
    the_spy = PushSpy(token='xoxp-fake', channel='')
    monkeypatch.setattr('thrds.cli.SlackClient', lambda *, token, channel: the_spy)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return the_spy


@pytest.fixture
def session(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 'T')
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n')
    (tmp_path / '02-beta.md').write_text('Beta OP.\n')
    SessionState.new(
        session_slug='s',
        staging_channel='C0STAGE',
        threads={'alpha': ThreadEntry(staging_ts='1.1'), 'beta': ThreadEntry(staging_ts='2.2')},
    ).save(tmp_path)
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'session')
    return tmp_path


STAGING = tracking.ref_name(tracking.STAGING)
REMOTE = tracking.ref_name(tracking.COMPOSITE)


def _push(*args, ok: bool = True):
    result = CliRunner().invoke(cli, ['slack', 'push', *args], catch_exceptions=False)
    if ok:
        assert result.exit_code == 0, result.stderr
    return result


def _seed(spy) -> None:
    """Fetch matching content so `staging` exists as a base."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = CliRunner().invoke(cli, ['slack', 'fetch'], catch_exceptions=False)
    assert result.exit_code == 0, result.stderr


# --- the gate ---


def test_push_refuses_when_staging_moved_since_last_pull(session, spy):
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    head_before = _git(session, 'rev-parse', 'HEAD')
    result = _push(ok=False)
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == (
        'Error: changed in staging since your last pull: 01-alpha.md. '
        '`slck pull` to reconcile, or `-f`/`--force` to overwrite.'
    )
    assert spy.synced == []
    # Refused before the commit, so nothing landed in history either.
    assert _git(session, 'rev-parse', 'HEAD') == head_before


def test_refusal_still_records_the_observation(session, spy):
    """The gate's fetch advances `staging` even when it refuses — it's
    an observation, and it's what lets `diff`/`pull` classify next."""
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    _push(ok=False)
    assert _git(session, 'show', f'{STAGING}:01-alpha.md') == 'Alpha OP, edited in Slack.'


def test_push_proceeds_when_local_already_matches_the_move(session, spy):
    """Slack moved, but to what we already have — nothing would be clobbered."""
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited in Slack.\n')
    result = _push()
    assert spy.synced == [['alpha', 'beta']]


def test_force_overwrites_deliberately(session, spy):
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    result = _push('-f')
    assert [l for l in result.stderr.splitlines() if l.startswith('--force')] == [
        '--force: overwriting staging changes in 01-alpha.md',
    ]
    assert spy.synced == [['alpha', 'beta']]


def test_first_push_without_a_fetch_proceeds(session, spy):
    """No base means nothing to gate against — and pushing is the explicit
    'local wins' bootstrap resolution, so it must go through."""
    spy.returns = {'alpha': thread('alpha', 'Whatever Slack had.'), 'beta': thread('beta', 'B.')}
    result = _push()
    assert spy.synced == [['alpha', 'beta']]


# --- commit-first + one commit per push ---


def test_push_commits_first_and_sends_the_commit(session, spy):
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    _push()
    assert _git(session, 'log', '--format=%s').splitlines() == [
        'thrds: push staging', 'session',
    ]
    assert _git(session, 'show', 'HEAD:01-alpha.md') == 'Alpha OP, edited locally.'


def test_state_folds_into_the_same_commit(session, spy):
    """One commit per push, working tree clean after — the state the push
    produced amends the content commit rather than trailing it."""
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    _push()
    assert _git(session, 'status', '--porcelain') == ''
    assert _git(session, 'log', '--format=%s').splitlines() == [
        'thrds: push staging', 'session',
    ]


# --- post-write verification ---


def test_refs_record_observed_state_after_push(session, spy):
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    _push()
    assert _git(session, 'show', f'{STAGING}:01-alpha.md') == 'Alpha OP, edited locally.'
    assert _git(session, 'diff', '--name-only', REMOTE, 'HEAD') == ''


def test_normalization_drift_warns_but_records_observed(session, spy):
    """Slack normalizing what we sent is recorded as-is: recording intent
    would make every later fetch report a spurious delta."""
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha *OP*, local.\n')
    spy.normalize = {'Alpha *OP*, local.': 'Alpha OP, local.'}
    result = _push()
    assert [l for l in result.stderr.splitlines() if l.startswith('note:')] == [
        "note: Slack's stored copy differs from what was sent (01-alpha.md); "
        'refs record the observed state',
    ]
    assert _git(session, 'show', f'{STAGING}:01-alpha.md') == 'Alpha OP, local.'


def test_a_fetch_right_after_a_push_is_a_nop(session, spy):
    """The property the whole gate rests on."""
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    _push()
    result = CliRunner().invoke(cli, ['slack', 'fetch'], catch_exceptions=False)
    assert result.stderr.splitlines() == [
        'staging: up to date',
        'prod: up to date',
        'upstream: up to date',
    ]


# --- dry run ---


def test_dry_run_neither_gates_nor_commits(session, spy):
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    head_before = _git(session, 'rev-parse', 'HEAD')
    result = _push('-n')
    assert spy.synced == [['alpha', 'beta']]
    assert _git(session, 'rev-parse', 'HEAD') == head_before
    # (_git strips, so porcelain's leading space is gone.)
    assert _git(session, 'status', '--porcelain').splitlines() == ['M 01-alpha.md']
