"""Tests for `slck fetch` — the read-only half of `pull`.

`pull` writes files and commits; `fetch` only records what Slack says, in refs
nothing is ever checked out from. The two properties worth pinning are that it
leaves the session alone, and that it applies the same staging/prod precedence
`pull` does — a ref built under different rules would be a merge base that
disagrees with the merge.
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


@dataclass
class PullSpy:
    """SlackClient stand-in returning canned staging / prod threads."""
    returns: dict[str, DocThread] = field(default_factory=dict)
    prod_returns: dict[str, DocThread] = field(default_factory=dict)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.prod_returns = {}

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def pull_threads_staging(self, state, session_dir=None, slugs=None, download_emoji=True):
        # `fetch` is an observation: it must never ask for emoji downloads.
        assert download_emoji is False
        return list(self.returns.values())

    def pull_promoted_threads(self, state, session_dir=None, slugs=None, download_emoji=True):
        assert download_emoji is False
        return list(self.prod_returns.values())


def thread(slug: str, *contents: str) -> DocThread:
    return DocThread(
        slug=slug,
        messages=[DocMessage(content=c) for c in contents],
    )


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


def _run(*args):
    return CliRunner().invoke(cli, ['slack', 'fetch', *args], catch_exceptions=False)


STAGING = tracking.ref_name(tracking.STAGING)
PROD = tracking.ref_name(tracking.PROD)
REMOTE = tracking.ref_name(tracking.COMPOSITE)


def _promote(session_dir: Path, slug: str) -> None:
    state = SessionState.load(session_dir)
    entry = state.thread(slug)
    entry.state = 'posted'
    entry.target = ThreadTarget(channel='C0PROD', thread_ts='0.1')
    entry.posted_msg_ts = ['7.7']
    state.save(session_dir)


def test_first_fetch_reports_each_ref(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        'staging: initialized (2 files)',
        'prod: initialized empty',
        'upstream: initialized (3 files)',
    ]


def test_refetching_unchanged_state_is_a_nop(session, spy):
    """The property `push` will gate on: a moved ref means Slack moved."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    at_first = _git(session, 'rev-parse', REMOTE)
    result = _run()
    assert result.stderr.splitlines() == [
        'staging: up to date',
        'prod: up to date',
        'upstream: up to date',
    ]
    assert _git(session, 'rev-parse', REMOTE) == at_first


def test_fetch_records_a_slack_side_edit(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    spy.returns['alpha'] = thread('alpha', 'Alpha OP, edited in Slack.')
    assert _run().stderr.splitlines()[0] == 'staging: 1 file (01-alpha.md)'
    assert _git(session, 'show', f'{STAGING}:01-alpha.md') == 'Alpha OP, edited in Slack.'


def test_composite_takes_prod_over_staging_for_a_posted_thread(session, spy):
    """The same precedence `pull` applies. A merge base built under different
    rules would disagree with the merge."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()  # confirmed bootstrap
    _promote(session, 'alpha')
    spy.returns['alpha'] = thread('alpha', 'Frozen staging copy.')
    spy.prod_returns = {'alpha': thread('alpha', 'Live prod copy.')}
    _run()
    assert _git(session, 'show', f'{REMOTE}:01-alpha.md') == 'Live prod copy.'
    assert _git(session, 'show', f'{STAGING}:01-alpha.md') == 'Frozen staging copy.'


def test_sources_answer_whether_the_staged_copy_has_drifted(session, spy):
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Frozen staging copy.')}
    spy.prod_returns = {'alpha': thread('alpha', 'Live prod copy.')}
    _run()
    assert _git(session, 'diff', '--name-only', STAGING, PROD) == '01-alpha.md'


def test_fetch_leaves_the_session_untouched(session, spy):
    before = (
        _git(session, 'rev-parse', 'HEAD'),
        _git(session, 'status', '--porcelain'),
        (session / '01-alpha.md').read_text(),
    )
    spy.returns = {'alpha': thread('alpha', 'Wildly different.')}
    _run()
    assert (
        _git(session, 'rev-parse', 'HEAD'),
        _git(session, 'status', '--porcelain'),
        (session / '01-alpha.md').read_text(),
    ) == before


def test_dry_run_moves_nothing(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.')}
    result = _run('-n')
    assert result.stderr.splitlines()[0] == (
        '[dry-run] staging: initialized (1 file)'
    )
    assert _git(session, 'for-each-ref', 'refs/remotes/') == ''
    assert _git(session, 'for-each-ref', 'refs/heads/upstream') == ''


def test_a_thread_deleted_in_slack_leaves_the_tree(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    spy.returns['alpha'] = thread('alpha')  # OP deleted: no messages
    assert _run().stderr.splitlines()[0] == 'staging: 1 file (01-alpha.md)'
    assert _git(session, 'ls-tree', '--name-only', STAGING) == '02-beta.md'


def test_head_is_the_first_snapshots_parent(session, spy):
    """So `upstream..HEAD` starts empty: nothing local to replay."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    assert _git(session, 'rev-list', '--count', f'{REMOTE}..HEAD') == '0'


def test_legacy_session_is_refused(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    SessionState.new(session_slug='s', staging_channel='C0S', doc_path='d.md').save(tmp_path)
    result = CliRunner().invoke(cli, ['slack', 'fetch'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Remote-tracking refs are per-thread only; run '
        '`thrds slack migrate` first.'
    )


def test_non_git_session_is_refused(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    SessionState.new(session_slug='s', staging_channel='C0S').save(tmp_path)
    result = CliRunner().invoke(cli, ['slack', 'fetch'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f'Error: {tmp_path} is not a git repo; nothing to track refs in.'
    )


def test_composite_preserves_files_slack_knows_nothing_about(session, spy):
    """The merge base is HEAD's tree with threads overlaid, not the threads
    alone. `README.md`, `thrds.json` and `emoji-*.png` have no Slack-side
    counterpart, and a base that omitted them would have `pull`'s
    `git rebase --onto` delete them — a rebase replays commits onto the new
    tree, so whatever isn't there is gone."""
    (session / 'README.md').write_text('Session notes.\n')
    _git(session, 'add', 'README.md')
    _git(session, 'commit', '-q', '-m', 'notes')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    assert sorted(_git(session, 'ls-tree', '--name-only', REMOTE).splitlines()) == [
        '01-alpha.md', '02-beta.md', 'README.md', 'thrds.json',
    ]
    assert _git(session, 'diff', '--name-only', REMOTE, 'HEAD') == ''


def test_composite_drops_a_thread_slack_no_longer_has(session, spy):
    """Pruning is scoped to thread files, so an unrelated file is never a
    candidate for removal."""
    (session / 'README.md').write_text('Session notes.\n')
    _git(session, 'add', 'README.md')
    _git(session, 'commit', '-q', '-m', 'notes')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()  # confirmed bootstrap
    del spy.returns['beta']
    _run()
    assert sorted(_git(session, 'ls-tree', '--name-only', REMOTE).splitlines()) == [
        '01-alpha.md', 'README.md', 'thrds.json',
    ]


def test_sources_stay_sparse(session, spy):
    """A source ref is an observation: `prod` holds the posted threads
    and nothing else, because that's all the target channel has."""
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'A.'), 'beta': thread('beta', 'B.')}
    spy.prod_returns = {'alpha': thread('alpha', 'A live.')}
    _run()
    assert _git(session, 'ls-tree', '--name-only', PROD) == '01-alpha.md'


def test_drift_between_the_staged_copy_and_what_is_live(session, spy):
    """`--diff-filter=M` is what makes this readable: both source refs are
    sparse in different ways, so plain `diff staging prod` reports every draft
    as "deleted from prod". Restricting to files present in *both* leaves
    exactly the posted threads whose staged copy has drifted."""
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Frozen.'), 'beta': thread('beta', 'B.')}
    spy.prod_returns = {'alpha': thread('alpha', 'Live.')}
    _run()
    assert _git(
        session, 'diff', '--name-only', '--diff-filter=M', STAGING, PROD,
    ) == '01-alpha.md'


def test_ambiguous_bootstrap_withholds_the_composite(session, spy):
    """A first fetch that disagrees with HEAD has no evidence of which side is
    ahead — 'remote ahead' reverts unpushed local work, 'local ahead' pushes
    stale content over a newer remote. Sources are observations and always
    land; the merge base is a claim, and an unconfirmed claim isn't made."""
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, who knows whose.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run()
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        'staging: initialized (2 files)',
        'prod: initialized empty',
        'upstream: not set — first fetch and HEAD disagree (01-alpha.md); '
        'resolve with `slck pull -m overwrite` (Slack wins) or `slck push` '
        '(local wins)',
    ]
    assert _git(session, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/') == (
        'prod\nstaging'
    )
    assert _git(session, 'for-each-ref', 'refs/heads/upstream') == ''


# --- per-remote fetch (`slck fetch <remote>...`) ---


def _bootstrap_and_promote(session: Path, spy) -> None:
    """Confirmed bootstrap, then alpha posted with its prod state recorded."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    _promote(session, 'alpha')
    spy.prod_returns = {'alpha': thread('alpha', 'Live prod copy.')}
    _run()


def test_fetch_staging_leaves_prod_and_its_composite_share_alone(session, spy):
    _bootstrap_and_promote(session, spy)
    prod_before = _git(session, 'rev-parse', PROD)
    spy.returns['beta'] = thread('beta', 'Beta OP, edited in Slack.')
    # A prod read now would report alpha gone; if `fetch staging` made one,
    # the assertions below would see alpha vanish from prod and the composite.
    spy.prod_returns = {}
    result = _run('staging')
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        'staging: 1 file (02-beta.md)',
        'prod: skipped (last observation kept)',
        'upstream: 1 file (02-beta.md)',
    ]
    assert _git(session, 'rev-parse', PROD) == prod_before
    assert _git(session, 'show', f'{REMOTE}:01-alpha.md') == 'Live prod copy.'
    assert _git(session, 'show', f'{REMOTE}:02-beta.md') == 'Beta OP, edited in Slack.'


def test_fetch_prod_leaves_staging_and_its_composite_share_alone(session, spy):
    _bootstrap_and_promote(session, spy)
    staging_before = _git(session, 'rev-parse', STAGING)
    spy.prod_returns = {'alpha': thread('alpha', 'Live, hand-edited.')}
    # A staging read now would report every draft gone.
    spy.returns = {}
    result = _run('prod')
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        'staging: skipped (last observation kept)',
        'prod: 1 file (01-alpha.md)',
        'upstream: 1 file (01-alpha.md)',
    ]
    assert _git(session, 'rev-parse', STAGING) == staging_before
    assert _git(session, 'show', f'{REMOTE}:01-alpha.md') == 'Live, hand-edited.'
    assert _git(session, 'show', f'{REMOTE}:02-beta.md') == 'Beta OP.'


def test_unknown_remote_is_refused(session, spy):
    result = _run('bogus')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: unknown remote(s): bogus; this session has: staging, prod.'
    )


def test_the_composite_is_not_fetchable_by_name(session, spy):
    result = _run('upstream')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: unknown remote(s): upstream; this session has: staging, prod. "
        "('upstream' is the derived merge base — it refreshes on every fetch, "
        "not by name)"
    )


def test_partial_fetch_refuses_when_the_skipped_remote_was_never_fetched(session, spy):
    """A never-fetched remote that has threads can't be skipped: with no
    stored ref, its threads would be misrecorded as deleted in the composite."""
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run('staging')
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: prod has threads but has never been fetched; skipping '
        'it would misrecord them as deleted in the composite. Run a full '
        '`slck fetch` first.'
    )
    assert _git(session, 'for-each-ref', 'refs/remotes/') == ''


def test_partial_fetch_proceeds_when_the_skipped_remote_holds_nothing(session, spy):
    """No posted threads = an unset prod ref and an empty prod are the same
    fact; nothing is at risk of being misrecorded."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run('staging')
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        'staging: initialized (2 files)',
        'prod: skipped (never fetched)',
        'upstream: initialized (3 files)',
    ]
    assert _git(session, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/') == (
        'staging'
    )
    assert _git(session, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/') == (
        'main\nupstream'
    )


def test_legacy_refs_migrate_on_first_touch(session, spy):
    """A session written under `refs/remotes/slack/{staging,prod,remote}`
    moves to the converged layout the first time any refs verb runs."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    _run()
    for name in ('staging', 'prod', 'remote'):
        old_style = (
            _git(session, 'rev-parse', STAGING if name == 'staging'
                 else PROD if name == 'prod' else REMOTE)
        )
        _git(session, 'update-ref', f'refs/remotes/slack/{name}', old_style)
    _git(session, 'update-ref', '-d', STAGING)
    _git(session, 'update-ref', '-d', PROD)
    _git(session, 'update-ref', '-d', REMOTE)
    result = _run()
    assert result.stderr.splitlines() == [
        'ref migrated: refs/remotes/slack/staging → refs/remotes/staging',
        'ref migrated: refs/remotes/slack/prod → refs/remotes/prod',
        'ref migrated: refs/remotes/slack/remote → refs/heads/upstream',
        'staging: up to date',
        'prod: up to date',
        'upstream: up to date',
    ]
    assert _git(session, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/') == (
        'prod\nstaging'
    )
