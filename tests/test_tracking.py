"""Tests for `thrds.tracking` — remote-tracking refs for a pseudo-remote.

A Slack channel keeps no version of itself, so the session repo keeps one for
it. These cover the properties `pull` and `push` will build on: a fetch is a
nop when nothing moved, it never touches the working tree, and the ref's
history *is* the remote's history.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from thrds import tracking


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 'T')
    (tmp_path / '01-a.md').write_text('local A\n')
    _git(tmp_path, 'add', '01-a.md')
    _git(tmp_path, 'commit', '-q', '-m', 'local')
    return tmp_path


REF = tracking.ref_name('slack', tracking.STAGING)


def _fetch(repo: Path, files: dict[str, str], write: bool = True):
    tree = tracking.build_tree(repo, files)
    return tracking.snapshot(repo, REF, 'slack/staging', tree, 'thrds: fetch staging', write=write)


# --- ref naming ---


def test_refs_live_under_remotes_so_they_get_reflogs():
    """`core.logAllRefUpdates` covers refs/remotes but not an invented
    namespace — and a ref with no reflog can't answer "what did Slack look
    like before this fetch"."""
    assert tracking.ref_name('slack', 'staging') == 'refs/remotes/slack/staging'


def test_short_ref_is_what_git_prints():
    assert tracking.short_ref('discord', 'prod') == 'discord/prod'


# --- first fetch ---


def test_first_fetch_records_observed_state(repo):
    snap = _fetch(repo, {'01-a.md': 'remote A\n'})
    assert (snap.changed, snap.base, snap.paths) == (True, None, ('01-a.md',))
    assert _git(repo, 'show', f'{REF}:01-a.md') == 'remote A'


def test_first_fetch_parents_on_head_rather_than_assuming_it(repo):
    """A bootstrap that can't look has to assume local == remote and warn.
    We can look, so the ref starts honest — and parenting on HEAD keeps
    `<ref>..HEAD` empty, i.e. nothing local to replay."""
    snap = _fetch(repo, {'01-a.md': 'remote A\n'})
    head = _git(repo, 'rev-parse', 'HEAD')
    assert _git(repo, 'rev-parse', f'{snap.sha}^') == head
    assert _git(repo, 'rev-list', '--count', f'{REF}..HEAD') == '0'


def test_first_fetch_of_an_empty_remote_still_creates_the_ref(repo):
    """"We looked, and Slack has nothing" is a different fact from "nothing
    changed", and a ref that exists is what later fetches compare against."""
    snap = _fetch(repo, {})
    assert (snap.changed, snap.summary()) == (True, 'initialized empty')
    assert _git(repo, 'ls-tree', '--name-only', REF) == ''


def test_fetch_works_in_a_repo_with_no_commits(tmp_path):
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 'T')
    snap = _fetch(tmp_path, {'01-a.md': 'remote A\n'})
    assert snap.changed is True
    assert _git(tmp_path, 'rev-list', '--count', snap.sha) == '1'


# --- nop semantics ---


def test_second_fetch_of_identical_state_is_a_nop(repo):
    """The property `push` gates on: a fetch right after a push changes
    nothing, so a moved ref means Slack really moved."""
    first = _fetch(repo, {'01-a.md': 'remote A\n'})
    again = _fetch(repo, {'01-a.md': 'remote A\n'})
    assert (again.changed, again.paths) == (False, ())
    assert again.sha == first.sha
    assert _git(repo, 'rev-parse', REF) == first.sha


def test_second_fetch_records_only_what_moved(repo):
    _fetch(repo, {'01-a.md': 'remote A\n', '02-b.md': 'remote B\n'})
    snap = _fetch(repo, {'01-a.md': 'remote A\n', '02-b.md': 'edited in slack\n'})
    assert (snap.changed, snap.paths) == (True, ('02-b.md',))


def test_a_deleted_thread_is_absent_not_empty(repo):
    """`serialize_thread` of a thread with no messages is a lone newline, which
    would read as "one blank line remains" rather than "it's gone"."""
    _fetch(repo, {'01-a.md': 'remote A\n', '02-b.md': 'remote B\n'})
    snap = _fetch(repo, {'01-a.md': 'remote A\n'})
    assert snap.paths == ('02-b.md',)
    assert _git(repo, 'ls-tree', '--name-only', REF) == '01-a.md'


def test_the_ref_history_is_the_remotes_history(repo):
    """`git show <ref>` is "what changed on Slack since I last looked"."""
    _fetch(repo, {'01-a.md': 'v1\n'})
    _fetch(repo, {'01-a.md': 'v2\n'})
    assert _git(repo, 'log', '--format=%s', REF).splitlines() == [
        'thrds: fetch staging', 'thrds: fetch staging', 'local',
    ]


# --- side-effect freedom ---


def test_fetch_never_touches_the_working_tree_or_branch(repo):
    before = (
        _git(repo, 'rev-parse', 'HEAD'),
        _git(repo, 'status', '--porcelain'),
        (repo / '01-a.md').read_text(),
    )
    _fetch(repo, {'01-a.md': 'wildly different\n'})
    assert (
        _git(repo, 'rev-parse', 'HEAD'),
        _git(repo, 'status', '--porcelain'),
        (repo / '01-a.md').read_text(),
    ) == before


def test_dry_run_leaves_the_ref_where_it_was(repo):
    _fetch(repo, {'01-a.md': 'v1\n'})
    at_v1 = _git(repo, 'rev-parse', REF)
    snap = _fetch(repo, {'01-a.md': 'v2\n'}, write=False)
    assert snap.changed is True
    assert _git(repo, 'rev-parse', REF) == at_v1


def test_dry_run_still_builds_a_showable_commit(repo):
    """So `-n` can print the diff it would have applied."""
    _fetch(repo, {'01-a.md': 'v1\n'})
    snap = _fetch(repo, {'01-a.md': 'v2\n'}, write=False)
    assert _git(repo, 'show', f'{snap.sha}:01-a.md') == 'v2'


def test_no_scratch_worktree_is_left_behind(repo):
    _fetch(repo, {'01-a.md': 'remote A\n'})
    assert _git(repo, 'worktree', 'list').splitlines() == [
        f'{repo} {_git(repo, "rev-parse", "--short", "HEAD")} [main]'
    ]


# --- sources are independent ---


def test_sources_track_separately(repo):
    """Drafts live only in staging, posted threads are canonical in prod — so
    each source tree is partial, and only the composite is a merge base."""
    staging = tracking.ref_name('slack', tracking.STAGING)
    prod = tracking.ref_name('slack', tracking.PROD)
    tracking.snapshot(repo, staging, 's', tracking.build_tree(repo, {'01-a.md': 'frozen draft\n'}), 'm')
    tracking.snapshot(repo, prod, 'p', tracking.build_tree(repo, {'01-a.md': 'live copy\n'}), 'm')
    assert tracking.changed_paths(repo, staging, prod) == ('01-a.md',)


# --- reporting ---


def test_summary_of_a_first_fetch_counts_without_listing(repo):
    """Nothing to have changed *from*, so every file is "new" and naming them
    says nothing."""
    assert _fetch(repo, {'01-a.md': 'x\n', '02-b.md': 'y\n'}).summary() == (
        'initialized (2 files)'
    )


def test_summary_names_the_changed_files(repo):
    _fetch(repo, {'01-a.md': 'x\n'})
    assert _fetch(repo, {'01-a.md': 'x2\n', '02-b.md': 'y\n'}).summary() == (
        '2 files (01-a.md, 02-b.md)'
    )


def test_summary_counts_what_changed_not_what_exists(repo):
    _fetch(repo, {'01-a.md': 'x\n'})
    assert _fetch(repo, {'01-a.md': 'x\n', '02-b.md': 'y\n'}).summary() == (
        '1 file (02-b.md)'
    )


def test_summary_of_an_unchanged_fetch(repo):
    _fetch(repo, {'01-a.md': 'x\n'})
    assert _fetch(repo, {'01-a.md': 'x\n'}).summary() == 'up to date'
