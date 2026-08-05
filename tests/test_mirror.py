"""Tests for `thrds/mirror.py` — git + gist subprocess wrappers.

Uses real git in tmp dirs (git is a hard dep for this feature; no reason
to fake it). Mocks the `gh` invocation via subprocess patching since we
can't create a real gist during tests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from thrds import mirror
from thrds.mirror import (
    MirrorError,
    add_remote,
    commit,
    commit_and_push,
    create_gist,
    find_git_root,
    has_remote,
    init_repo,
    is_git_repo,
    push,
    resolve_session_dir,
)


@pytest.fixture
def session_dir(tmp_path):
    """A tmp dir with `git init` already run — ready for commit tests."""
    d = tmp_path / 'session'
    d.mkdir()
    init_repo(d)
    # Every commit needs an author configured on the fresh repo.
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=d, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=d, check=True)
    return d


# --- helpers ---

def test_is_git_repo(tmp_path):
    assert is_git_repo(tmp_path) is False
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    assert is_git_repo(tmp_path) is True


def test_find_git_root_walks_up(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    nested = tmp_path / 'a' / 'b'
    nested.mkdir(parents=True)
    assert find_git_root(nested) == tmp_path


def test_find_git_root_returns_none_outside_repo(tmp_path):
    # tmp_path is not a git repo and (in test envs) shouldn't be inside one.
    assert find_git_root(tmp_path) is None


def test_resolve_session_dir_uses_git_root(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    nested = tmp_path / 'sub'
    nested.mkdir()
    assert resolve_session_dir(nested, 'trainium') == tmp_path / 'thrds' / 'trainium'


def test_resolve_session_dir_falls_back_to_cwd_outside_repo(tmp_path):
    assert resolve_session_dir(tmp_path, 'trainium') == tmp_path / 'thrds' / 'trainium'


# --- init_repo ---

def test_init_repo_creates_git_dir(tmp_path):
    d = tmp_path / 'x'
    d.mkdir()
    init_repo(d)
    assert (d / '.git').is_dir()


def test_init_repo_is_idempotent(tmp_path):
    d = tmp_path / 'x'
    d.mkdir()
    init_repo(d)
    init_repo(d)  # no raise
    assert (d / '.git').is_dir()


def test_init_repo_uses_main_branch(session_dir):
    """Fresh init defaults to `main` (git 2.28+); pinned so `push HEAD:main` works everywhere."""
    (session_dir / 'x.txt').write_text('x')
    commit(session_dir, ['x.txt'], 'first')
    branch = subprocess.run(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == 'main'


# --- commit ---

def test_commit_stages_and_returns_sha(session_dir):
    (session_dir / 'foo.md').write_text('body')
    sha = commit(session_dir, ['foo.md'], 'add foo')
    assert sha is not None and len(sha) == 40


def test_commit_returns_none_when_nothing_staged(session_dir):
    """Idempotent: no changes → no commit, no error."""
    (session_dir / 'foo.md').write_text('body')
    commit(session_dir, ['foo.md'], 'first')
    sha = commit(session_dir, ['foo.md'], 'again')
    assert sha is None


def test_commit_message_lands_in_git_log(session_dir):
    (session_dir / 'foo.md').write_text('body')
    commit(session_dir, ['foo.md'], 'commit message here')
    log = subprocess.run(
        ['git', 'log', '--format=%s', '-n', '1'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == 'commit message here'


# --- remote handling ---

def test_add_and_check_remote(session_dir):
    assert has_remote(session_dir, 'g') is False
    add_remote(session_dir, 'g', 'https://gist.github.com/abc.git')
    assert has_remote(session_dir, 'g') is True


def test_add_remote_updates_existing(session_dir):
    add_remote(session_dir, 'g', 'https://gist.github.com/abc.git')
    add_remote(session_dir, 'g', 'https://gist.github.com/xyz.git')
    url = subprocess.run(
        ['git', 'remote', 'get-url', 'g'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert url == 'https://gist.github.com/xyz.git'


def test_push_noop_when_no_remote(session_dir):
    """No `g` remote → push() returns silently, no error."""
    push(session_dir, remote='g')  # no raise


# --- create_gist (subprocess-mocked) ---

def test_create_gist_parses_url_from_gh_stdout(session_dir, monkeypatch):
    """`gh gist create --secret` stdout is a URL; parse it to (gist_id, git_url)."""
    captured: list[list[str]] = []

    def fake_run(cmd, cwd=None, check=True, capture_output=True, text=True):
        captured.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout='https://gist.github.com/abc123def456\n',
            stderr='',
        )
    monkeypatch.setattr(mirror.subprocess, 'run', fake_run)

    gid, git_url = create_gist(session_dir, description='thrds: trainium', files=['trainium.md'])
    assert gid == 'abc123def456'
    assert git_url == 'https://gist.github.com/abc123def456.git'
    assert captured == [[
        'gh', 'gist', 'create', '--secret',
        '--desc', 'thrds: trainium', 'trainium.md',
    ]]


def test_create_gist_raises_mirror_error_on_gh_failure(session_dir, monkeypatch):
    """gh exit != 0 → MirrorError with the stderr embedded."""
    def fake_run(cmd, cwd=None, check=True, capture_output=True, text=True):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, output='', stderr='gh: auth required',
        )
    monkeypatch.setattr(mirror.subprocess, 'run', fake_run)

    with pytest.raises(MirrorError, match=r'gh: auth required'):
        create_gist(session_dir, description='x', files=['foo.md'])


# --- commit_and_push ---

def test_commit_and_push_commits_locally_without_remote(session_dir):
    """No `g` remote: commit still fires, push is a no-op, no error."""
    (session_dir / 'foo.md').write_text('body')
    sha = commit_and_push(session_dir, ['foo.md'], 'add foo')
    assert sha is not None
    # Assert HEAD advanced (commit succeeded).
    head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == sha


def test_commit_and_push_returns_none_when_no_changes(session_dir):
    (session_dir / 'foo.md').write_text('body')
    commit_and_push(session_dir, ['foo.md'], 'add foo')
    sha2 = commit_and_push(session_dir, ['foo.md'], 're-add foo')
    assert sha2 is None
