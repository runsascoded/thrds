"""Tests for history replay (`thrds.replay`) — legacy doc history → per-thread files."""
from __future__ import annotations

import json
import subprocess

import pytest

from thrds.migrate import PREAMBLE_SLUG
from thrds.replay import (
    ReplayError,
    assign_indices,
    commits_in,
    plan_replay,
    replay,
    verify_plan,
)


# --- assign_indices: the subtle part ---


def test_indices_follow_final_commit_order():
    assert assign_indices([['a', 'b']]) == {'a': 1, 'b': 2, PREAMBLE_SLUG: 0}


def test_indices_are_stable_across_an_insertion():
    """The case that motivates global assignment: `b` is inserted at position 2
    in the last commit. Numbering per-commit would move `c` from 2 to 3 —
    renaming its file and severing its history."""
    idx = assign_indices([['a', 'c'], ['a', 'b', 'c']])
    assert idx == {'a': 1, 'b': 2, 'c': 3, PREAMBLE_SLUG: 0}


def test_earlier_commit_leaves_a_gap_rather_than_renumbering():
    """`c` keeps index 3 even in the commit where `b` doesn't exist yet."""
    idx = assign_indices([['a', 'c'], ['a', 'b', 'c']])
    first_commit_files = sorted(idx[s] for s in ['a', 'c'])
    assert first_commit_files == [1, 3]


def test_cut_thread_appended_after_survivors():
    """A thread present early but gone by the end must not displace a survivor."""
    idx = assign_indices([['a', 'gone'], ['a', 'b']])
    assert idx == {'a': 1, 'b': 2, 'gone': 3, PREAMBLE_SLUG: 0}


def test_multiple_cut_threads_ordered_most_recent_first():
    idx = assign_indices([['old'], ['mid'], ['keep']])
    assert idx == {'keep': 1, 'mid': 2, 'old': 3, PREAMBLE_SLUG: 0}


def test_preamble_always_index_zero():
    assert assign_indices([['a']])[PREAMBLE_SLUG] == 0


def test_empty_history_still_reserves_preamble():
    assert assign_indices([]) == {PREAMBLE_SLUG: 0}


def test_duplicate_slug_across_commits_assigned_once():
    idx = assign_indices([['a', 'b'], ['a', 'b']])
    assert idx == {'a': 1, 'b': 2, PREAMBLE_SLUG: 0}


# --- repo fixture ---


def _git(repo, *args, **kw):
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True, **kw,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A legacy session repo whose doc gains threads over three commits."""
    d = tmp_path / 'session'
    d.mkdir()
    _git(d, 'init', '-q', '-b', 'main')
    _git(d, 'config', 'user.email', 'test@example.com')
    _git(d, 'config', 'user.name', 'Test')
    (d / 'thrds.json').write_text(json.dumps({
        'session_id': 'sid', 'doc_path': 'draft.md',
        'staging_threads': {'a': '1.1', 'b': '2.2'},
    }) + '\n')

    def commit(doc_text, msg):
        (d / 'draft.md').write_text(doc_text)
        _git(d, 'add', 'draft.md', 'thrds.json')
        _git(d, 'commit', '-q', '-m', msg)

    commit('Just a preamble.\n', 'v1: single message')
    commit('=== a\n\nA body.\n\n=== c\n\nC body.\n', 'v2: two threads')
    commit('Intro.\n\n=== a\n\nA body v3.\n\n=== b\n\nB body.\n\n=== c\n\nC body.\n', 'v3: insert b')
    return d


# --- plan_replay ---


def test_plan_covers_every_commit(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert [p.subject for p in plans] == [
        'v1: single message', 'v2: two threads', 'v3: insert b',
    ]


def test_plan_file_names_per_commit(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert [p.md_names for p in plans] == [
        ['00-preamble.md'],
        ['01-a.md', '03-c.md'],
        ['00-preamble.md', '01-a.md', '02-b.md', '03-c.md'],
    ]


def test_plan_leaves_gap_where_thread_not_yet_introduced(repo):
    """v2 has no `02-` file — `b` doesn't exist yet, and `c` keeps its number."""
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert plans[1].md_names == ['01-a.md', '03-c.md']


def test_plan_splits_content_verbatim(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert plans[2].new_files['01-a.md'] == 'A body v3.\n'
    assert plans[2].new_files['00-preamble.md'] == 'Intro.\n'


def test_plan_drops_the_legacy_doc(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert 'draft.md' not in plans[2].new_files
    assert 'draft.md' not in plans[2].keep


def test_plan_rewrites_state_into_per_thread_shape(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    state = json.loads(plans[2].new_files['thrds.json'])
    assert state['doc_path'] is None
    assert state['session_slug'] == 'draft'
    assert state['staging_threads'] == {}
    assert sorted(state['threads']) == ['a', 'b', 'c', 'preamble']


def test_plan_carries_staging_ts_into_thread_entries(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    state = json.loads(plans[2].new_files['thrds.json'])
    assert state['threads']['a']['remotes']['staging']['ts'] == '1.1'
    assert state['threads']['c']['remotes'] == {}


def test_plan_unknown_ref_raises(repo):
    with pytest.raises(ReplayError):
        plan_replay(repo, 'nope', 'draft.md')


def test_plan_missing_doc_raises(repo):
    with pytest.raises(ReplayError) as e:
        plan_replay(repo, 'HEAD', 'other.md')
    assert str(e.value) == "No commit reachable from 'HEAD' contains 'other.md'."


# --- verify_plan ---


def test_verify_passes_on_a_faithful_plan(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    assert verify_plan(repo, plans, 'draft.md') == []


def test_verify_catches_lost_thread_content(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    plans[2].new_files['01-a.md'] = 'Corrupted.\n'
    assert verify_plan(repo, plans, 'draft.md') == [
        f'{plans[2].sha[:8]}: thread \'a\' differs after split',
    ]


def test_verify_catches_lost_preamble(repo):
    plans, _ = plan_replay(repo, 'HEAD', 'draft.md')
    del plans[2].new_files['00-preamble.md']
    assert verify_plan(repo, plans, 'draft.md') == [
        f'{plans[2].sha[:8]}: preamble differs after split',
    ]


# --- write_replay / replay ---


def test_replay_writes_branch_with_same_commit_count(repo):
    result = replay(repo, 'draft.md', branch='per-thread')
    assert len(commits_in(repo, 'per-thread')) == 3
    assert result.branch == 'per-thread'


def test_replay_preserves_commit_messages(repo):
    replay(repo, 'draft.md', branch='per-thread')
    subjects = _git(repo, 'log', '--format=%s', '--reverse', 'per-thread').strip().split('\n')
    assert subjects == ['v1: single message', 'v2: two threads', 'v3: insert b']


def test_replay_preserves_author_and_dates(repo):
    before = _git(repo, 'log', '--format=%an|%ae|%aI|%cI', '--reverse', 'main').strip()
    replay(repo, 'draft.md', branch='per-thread')
    after = _git(repo, 'log', '--format=%an|%ae|%aI|%cI', '--reverse', 'per-thread').strip()
    assert after == before


def test_replay_tree_contents_per_commit(repo):
    replay(repo, 'draft.md', branch='per-thread')
    shas = commits_in(repo, 'per-thread')
    trees = [
        sorted(_git(repo, 'ls-tree', '--name-only', s).strip().split('\n'))
        for s in shas
    ]
    assert trees == [
        ['00-preamble.md', 'thrds.json'],
        ['01-a.md', '03-c.md', 'thrds.json'],
        ['00-preamble.md', '01-a.md', '02-b.md', '03-c.md', 'thrds.json'],
    ]


def test_replay_leaves_original_branch_untouched(repo):
    before = _git(repo, 'rev-parse', 'main').strip()
    replay(repo, 'draft.md', branch='per-thread')
    assert _git(repo, 'rev-parse', 'main').strip() == before


def test_replay_refuses_when_verification_fails(repo, monkeypatch):
    """A rewrite that would lose content must not reach the branch."""
    import thrds.replay as R
    monkeypatch.setattr(R, 'verify_plan', lambda *a, **k: ['boom: thread differs'])
    with pytest.raises(ReplayError) as e:
        replay(repo, 'draft.md', branch='per-thread')
    assert str(e.value) == (
        'Replay verification failed — the rewrite would lose content:\n  boom: thread differs'
    )
    assert subprocess.run(
        ['git', 'rev-parse', '--verify', 'per-thread'], cwd=repo, capture_output=True,
    ).returncode != 0


def test_replay_carries_binary_files_by_blob_sha(repo):
    """Binary blobs (downloaded emoji) must survive without being decoded."""
    png = repo / 'emoji-x.png'
    png.write_bytes(b'\x89PNG\r\n\x1a\n' + bytes(range(256)))
    _git(repo, 'add', 'emoji-x.png')
    _git(repo, 'commit', '-q', '-m', 'add emoji')
    before = _git(repo, 'rev-parse', 'HEAD:emoji-x.png').strip()
    replay(repo, 'draft.md', branch='per-thread')
    assert _git(repo, 'rev-parse', 'per-thread:emoji-x.png').strip() == before
