"""Tests for `slck promote`'s prod gate and post-write ref recording.

A first promote is append-only (empty `only_ids` scope) and ungated. A
re-promote converges our own posted messages — so if someone hand-edited one
at the target since our last pull, converging would silently overwrite it.
The gate compares the target's current copy against `slack/prod`'s record and
refuses. Foreign replies never trip it: only our own message ids are read.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget, tracking
from thrds.cli import SLACK_TOKEN_ENV, cli


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


class FakePlan:
    actions: list = []

    @staticmethod
    def format_preview(color: bool = True, prefix: str = '') -> str:
        return f'{prefix}POST [0]'


@dataclass
class PromoteSpy:
    prod_returns: dict[str, DocThread] = field(default_factory=dict)
    returns: dict[str, DocThread] = field(default_factory=dict)
    promoted: list[str] = field(default_factory=list)

    def __init__(self, *, token, channel):
        self.prod_returns = {}
        self.returns = {}
        self.promoted = []

    def list_channels_by_name(self) -> dict[str, str]:
        return {'prod': 'C0PROD'}

    def pull_threads_staging(self, state, session_dir=None, slugs=None):
        return list(self.returns.values())

    def pull_promoted_threads(self, state, session_dir=None, slugs=None):
        wanted = None if slugs is None else set(slugs)
        return [
            t for s, t in self.prod_returns.items()
            if wanted is None or s in wanted
        ]

    def promote_thread(self, slug, thread, target, state, dry_run=False, **kw):
        if not dry_run:
            self.promoted.append(slug)
            entry = state.thread(slug)
            entry.state = 'posted'
            entry.target = target
            entry.posted_ts = '9.9'
            entry.posted_msg_ts = ['9.9']
            self.prod_returns[slug] = DocThread(slug=slug, messages=list(thread.messages))
        return FakePlan()


def thread(slug: str, *contents: str) -> DocThread:
    return DocThread(slug=slug, messages=[DocMessage(content=c) for c in contents])


@pytest.fixture
def spy(monkeypatch):
    the_spy = PromoteSpy(token='xoxp-fake', channel='')
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
    SessionState.new(
        session_slug='s',
        staging_channel='C0STAGE',
        threads={'alpha': ThreadEntry(staging_ts='1.1', target=ThreadTarget(channel='C0PROD'))},
    ).save(tmp_path)
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'session')
    return tmp_path


PROD = tracking.ref_name('slack', tracking.PROD)
REMOTE = tracking.ref_name('slack', tracking.COMPOSITE)


def _promote(*args, ok: bool = True):
    result = CliRunner().invoke(
        cli, ['slack', 'promote', '-y', *args, 'alpha'], catch_exceptions=False,
    )
    if ok:
        assert result.exit_code == 0, result.stderr
    return result


def _mark_posted(session: Path, spy, text: str = 'Alpha OP.') -> None:
    """A prior promote, as state + Slack + refs would record it."""
    state = SessionState.load(session)
    entry = state.thread('alpha')
    entry.state = 'posted'
    entry.posted_ts = '9.9'
    entry.posted_msg_ts = ['9.9']
    state.save(session)
    spy.prod_returns['alpha'] = thread('alpha', text)
    # `slack/prod` records it, as the promote's verify (or a pull) would.
    tracking.snapshot(
        session, PROD, 'slack/prod',
        tracking.build_tree(session, {'01-alpha.md': f'{text}\n'}),
        'thrds: fetch prod',
    )


def test_first_promote_is_ungated(session, spy):
    """Append-only by construction — nothing of ours at the target yet."""
    result = _promote()
    assert spy.promoted == ['alpha']
    assert _git(session, 'show', f'{PROD}:01-alpha.md') == 'Alpha OP.'


def test_verify_records_prod_and_refreshes_the_composite(session, spy):
    _promote()
    assert _git(session, 'ls-tree', '--name-only', PROD) == '01-alpha.md'
    assert _git(session, 'ls-tree', '--name-only', REMOTE) != ''


def test_repromote_refuses_when_prod_was_hand_edited(session, spy):
    _mark_posted(session, spy)
    spy.prod_returns['alpha'] = thread('alpha', 'Alpha OP, hand-edited in prod.')
    result = _promote(ok=False)
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == (
        'Error: 01-alpha.md changed at the target since your last pull '
        '(hand-edited in prod?). `slck pull` to reconcile, or `-f`/`--force` '
        'to overwrite.'
    )
    assert spy.promoted == []


def test_refusal_records_the_observation(session, spy):
    """Like `push`'s gate: the fetch that refused still advances the ref, so
    the next `pull` sees the hand-edit as remote-only and applies it."""
    _mark_posted(session, spy)
    spy.prod_returns['alpha'] = thread('alpha', 'Alpha OP, hand-edited in prod.')
    _promote(ok=False)
    assert _git(session, 'show', f'{PROD}:01-alpha.md') == 'Alpha OP, hand-edited in prod.'


def test_repromote_proceeds_when_prod_matches_the_record(session, spy):
    _mark_posted(session, spy)
    result = _promote()
    assert spy.promoted == ['alpha']


def test_force_overrides_the_gate(session, spy):
    _mark_posted(session, spy)
    spy.prod_returns['alpha'] = thread('alpha', 'Alpha OP, hand-edited in prod.')
    result = _promote('-f')
    assert [l for l in result.stderr.splitlines() if l.startswith('--force')] == [
        '--force: overwriting prod changes in 01-alpha.md',
    ]
    assert spy.promoted == ['alpha']


def test_repromote_with_no_prod_ref_proceeds(session, spy):
    """Never fetched = nothing to gate against; promote is the explicit
    local-wins statement, mirroring `push`'s bootstrap rule."""
    state = SessionState.load(session)
    entry = state.thread('alpha')
    entry.state = 'posted'
    entry.posted_ts = '9.9'
    entry.posted_msg_ts = ['9.9']
    state.save(session)
    spy.prod_returns['alpha'] = thread('alpha', 'Alpha OP, hand-edited in prod.')
    result = _promote()
    assert spy.promoted == ['alpha']


def test_dry_run_stays_read_only(session, spy):
    result = CliRunner().invoke(
        cli, ['slack', 'promote', '-n', 'alpha'], catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert spy.promoted == []
    assert _git(session, 'for-each-ref', 'refs/remotes/') == ''
