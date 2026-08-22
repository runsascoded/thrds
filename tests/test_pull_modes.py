"""Tests for `slck pull`'s reconcile modes on a per-thread git session.

The old `pull` was unconditionally what `-m overwrite` now is: write what
Slack says, destroying unpushed local edits silently. With the `slack/*` refs
as a base, `rebase` (default) three-way merges instead — a local commit Slack
hasn't seen survives, a Slack edit lands, and both-changed-the-same-thread
aborts rather than writing conflict markers a later `push` would post.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, tracking
from thrds.cli import SLACK_TOKEN_ENV, cli


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@dataclass
class PullSpy:
    returns: dict[str, DocThread] = field(default_factory=dict)
    prod_returns: dict[str, DocThread] = field(default_factory=dict)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.prod_returns = {}

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def pull_thread_states(self, remote, state, session_dir=None, slugs=None, download_emoji=True):
        returns = self.returns if remote.name == 'staging' else self.prod_returns
        return list(returns.values())

    def pull_chrome_edits(self, state, filenames=None):
        return {}

    def adopt_new_staging_threads(self, state, session_dir):
        return []


def thread(slug: str, *contents: str) -> DocThread:
    return DocThread(slug=slug, messages=[DocMessage(content=c) for c in contents])


@pytest.fixture
def spy(monkeypatch):
    the_spy = PullSpy(token='xoxp-fake', channel='')
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


REMOTE = tracking.ref_name(tracking.COMPOSITE)


def _run(*args, ok: bool = True):
    result = CliRunner().invoke(cli, ['slack', 'pull', *args], catch_exceptions=False)
    if ok:
        assert result.exit_code == 0, result.stderr
    return result


def _seed(spy) -> None:
    """Confirmed bootstrap: fetch content matching HEAD, so the base exists."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()


def _commit_edit(session: Path, name: str, text: str) -> None:
    (session / name).write_text(text)
    _git(session, 'add', name)
    _git(session, 'commit', '-q', '-m', f'local edit {name}')


# --- rebase (default) ---


def test_slack_edit_lands(session, spy):
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    result = _run()
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited in Slack.\n'
    assert result.stderr.splitlines() == [
        'staging: 1 file (01-alpha.md)',
        'prod: up to date',
        'upstream: 1 file (01-alpha.md)',
        'updated 1: 01-alpha.md (local commits preserved)',
    ]
    assert _git(session, 'log', '--format=%s').splitlines() == [
        'thrds: pull staging', 'session',
    ]


def test_local_commit_survives_a_pull(session, spy):
    """The headline fix: the old pull would have reverted this edit."""
    _seed(spy)
    _commit_edit(session, '01-alpha.md', 'Alpha OP, edited locally.\n')
    result = _run()
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited locally.\n'
    assert result.stderr.splitlines()[-1] == 'no content changes from Slack'
    # The base tracks the remote, so the surviving edit reads as local-only.
    assert tracking.changed_paths(session, REMOTE, 'HEAD') == ('01-alpha.md',)


def test_both_sides_merge_when_touching_different_threads(session, spy):
    _seed(spy)
    _commit_edit(session, '02-beta.md', 'Beta OP, edited locally.\n')
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    result = _run()
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited in Slack.\n'
    assert (session / '02-beta.md').read_text() == 'Beta OP, edited locally.\n'
    assert result.stderr.splitlines()[-1] == (
        'updated 1: 01-alpha.md (local commits preserved)'
    )


def test_same_thread_changed_on_both_sides_conflicts(session, spy):
    """No conflict markers on disk: a later `push` would post them to Slack."""
    _seed(spy)
    _commit_edit(session, '01-alpha.md', 'Alpha OP, edited locally.\n')
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    base_before = _git(session, 'rev-parse', REMOTE)
    result = _run(ok=False)
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == (
        'Error: CONFLICT in 01-alpha.md — Slack and local commits both changed '
        'since the last fetch. `slck pull -m overwrite` (Slack wins), or '
        'resolve locally and `slck push` (local wins); `git diff staging '
        'HEAD` shows both sides.'
    )
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited locally.\n'
    # The base did NOT advance — advancing it would make the conflict
    # evaporate: the next pull would find base == remote and do nothing.
    assert _git(session, 'rev-parse', REMOTE) == base_before
    again = _run(ok=False)
    assert again.exit_code == 1
    assert again.stderr.splitlines()[-1] == result.stderr.splitlines()[-1]


def test_thread_deleted_in_slack_deletes_the_file(session, spy):
    _seed(spy)
    del spy.returns['beta']
    result = _run()
    assert not (session / '02-beta.md').exists()
    assert result.stderr.splitlines()[-1] == (
        'deleted 1: 02-beta.md (local commits preserved)'
    )


# --- other modes ---


def test_overwrite_clobbers_deliberately(session, spy):
    _seed(spy)
    _commit_edit(session, '01-alpha.md', 'Alpha OP, edited locally.\n')
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    _run('-m', 'overwrite')
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited in Slack.\n'


def test_merge_mode_records_the_fetched_snapshot_as_a_parent(session, spy):
    """The branch's history connects to the remote state it reconciled with."""
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    _run('-m', 'merge')
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, edited in Slack.\n'
    parents = _git(session, 'log', '-1', '--format=%P').split()
    assert len(parents) == 2
    assert _git(session, 'show', f'{parents[1]}:01-alpha.md') == 'Alpha OP, edited in Slack.'
    assert _git(session, 'log', '-1', '--format=%s') == 'thrds: pull staging'


def test_mode_defaults_from_git_config(session, spy):
    _seed(spy)
    _git(session, 'config', 'thrds.pullMode', 'overwrite')
    _commit_edit(session, '01-alpha.md', 'Alpha OP, edited locally.\n')
    _run()
    assert (session / '01-alpha.md').read_text() == 'Alpha OP.\n'


# --- guards ---


def test_dirty_tracked_files_abort_a_rebase(session, spy):
    _seed(spy)
    (session / '01-alpha.md').write_text('Alpha OP, uncommitted.\n')
    result = _run(ok=False)
    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        'Error: 1 file(s) with uncommitted changes; `-m rebase` reconciles '
        'committed state and would mix them in: 01-alpha.md. Commit them '
        '(or `-m overwrite`).'
    ]
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, uncommitted.\n'


def test_ambiguous_bootstrap_refuses_then_overwrite_resolves(session, spy):
    """No base and Slack ≠ HEAD: neither side is evidently ahead, so rebase
    refuses; `-m overwrite` is an explicit 'Slack wins', after which the base
    exists and pulls are ordinary."""
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, who knows whose.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run(ok=False)
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == (
        "Error: no sync base — this session's first fetch found Slack and "
        'HEAD disagreeing; resolve with `slck pull -m overwrite` (Slack wins) '
        'or `slck push` (local wins).'
    )
    assert (session / '01-alpha.md').read_text() == 'Alpha OP.\n'
    _run('-m', 'overwrite')
    assert (session / '01-alpha.md').read_text() == 'Alpha OP, who knows whose.\n'
    result = _run()
    assert result.stderr.splitlines()[-1] == 'no content changes from Slack'


def test_mode_flag_needs_a_git_session(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n')
    SessionState.new(
        session_slug='s', staging_channel='C0S',
        threads={'alpha': ThreadEntry(staging_ts='1.1')},
    ).save(tmp_path)
    result = CliRunner().invoke(cli, ['slack', 'pull', '-m', 'rebase'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: -m rebase needs a git session (no refs to reconcile against).'
    )


def test_dry_run_moves_nothing(session, spy):
    _seed(spy)
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    ref_before = _git(session, 'rev-parse', REMOTE)
    head_before = _git(session, 'rev-parse', 'HEAD')
    result = _run('-n')
    assert result.stdout == 'Alpha OP, edited in Slack.\nBeta OP.\n'
    assert (session / '01-alpha.md').read_text() == 'Alpha OP.\n'
    assert _git(session, 'rev-parse', REMOTE) == ref_before
    assert _git(session, 'rev-parse', 'HEAD') == head_before
