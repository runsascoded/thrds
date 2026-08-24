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
    by_name: dict[str, dict[str, DocThread]] = field(default_factory=dict)
    synced: list[list[str]] = field(default_factory=list)
    synced_remotes: list[str] = field(default_factory=list)
    normalize: dict[str, str] = field(default_factory=dict)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.prod_returns = {}
        self.by_name = {}
        self.synced = []
        self.synced_remotes = []
        self.normalize = {}

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def pull_thread_states(self, remote, state, session_dir=None, slugs=None, download_emoji=True):
        if remote.name in self.by_name:
            return list(self.by_name[remote.name].values())
        returns = self.returns if remote.name == 'staging' else self.prod_returns
        return list(returns.values())

    def pull_chrome_edits(self, state, filenames=None):
        return {}

    def adopt_new_staging_threads(self, state, session_dir):
        return []

    def sync_threads_staging(self, threads, state, dry_run=False, filenames=None, remote=None, **kw):
        self.synced.append([t.slug for t in threads])
        self.synced_remotes.append(remote.name if remote is not None else None)
        if not dry_run:
            stored = {
                t.slug: DocThread(slug=t.slug, messages=[
                    DocMessage(content=self.normalize.get(m.content, m.content))
                    for m in t.messages
                ])
                for t in threads
            }
            if remote is not None and remote.name in self.by_name:
                self.by_name[remote.name] = stored
            else:
                self.returns = stored
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


def test_re_running_push_still_refuses(session, spy):
    """The gate must not disarm itself by firing.

    A refusal advances the tracking ref deliberately — it's an observation,
    and it's what makes the post-refusal diff readable. So a gate phrased as
    "did anything change since I last looked?" writes down the very thing the
    retry compares against: the second push sees nothing new and overwrites
    the edit the first one refused to touch, though nothing about the danger
    changed between the two commands. A gate that clears by firing is worse
    than none, because the operator reads the refusal as protection and the
    retry as proof they fixed it. Only a reconcile may clear it.
    """
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    assert _push(ok=False).exit_code == 1
    second = _push(ok=False)
    assert second.exit_code == 1
    assert second.stderr.splitlines()[-1] == (
        'Error: changed in staging since your last pull: 01-alpha.md. '
        '`slck pull` to reconcile, or `-f`/`--force` to overwrite.'
    )
    assert spy.synced == []


@pytest.mark.parametrize('mode', ['rebase', 'merge', 'overwrite'])
def test_reconciling_is_what_clears_the_gate(session, spy, mode):
    """The complement of the test above: a refusal is cleared by incorporating
    the remote's state and by nothing else. Every reconcile mode qualifies,
    since each one leaves HEAD holding what the remote holds.

    The two sides touch *different* files, so `rebase`/`merge` have something
    to reconcile rather than a conflict to report — the gate still refuses,
    because Slack moved a file this push would rewrite.
    """
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    (session / '02-beta.md').write_text('Beta OP, edited locally.\n')
    _git(session, 'commit', '-qam', 'local edit')
    assert _push(ok=False).exit_code == 1

    pull = CliRunner().invoke(cli, ['slack', 'pull', '-m', mode], catch_exceptions=False)
    assert pull.exit_code == 0, pull.stderr
    _push()
    assert spy.synced == [['alpha', 'beta']]


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


# --- `push -r <remote>` ---


def test_push_r_unknown_remote_is_refused(session, spy):
    result = _push('-r', 'bogus', ok=False)
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: unknown remote 'bogus'; this session has: staging, prod."
    )
    assert spy.synced == []


def test_push_r_prod_role_is_refused(session, spy):
    result = _push('-r', 'prod', ok=False)
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: remote 'prod' is prod-role; `slck promote <slug>` posts a "
        'thread to its target.'
    )
    assert spy.synced == []


def test_push_r_syncs_gates_and_verifies_against_that_remote(session, spy):
    """`-r scratch` end to end: the sync targets the named remote, and the
    post-write verification records what it observed in *that* remote's ref —
    the default staging ref is never created."""
    state = SessionState.load(session)
    state.remotes = {'scratch': {'role': 'staging', 'channel': 'C0SCRATCH'}}
    state.save(session)
    _git(session, 'add', 'thrds.yml')
    _git(session, 'commit', '-q', '-m', 'declare scratch')
    spy.by_name = {'scratch': {}}
    result = _push('-r', 'scratch')
    assert spy.synced_remotes == ['scratch']
    assert _git(session, 'show', 'refs/remotes/scratch:01-alpha.md') == 'Alpha OP.'
    assert _git(
        session, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/',
    ) == 'scratch'
