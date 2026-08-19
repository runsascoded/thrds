"""Tests for `refs/ghpr/remote`, `ghpr fetch`, and `ghpr pull`'s reconcile modes."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from utz import proc

from ghpr import refs
from ghpr.commands import fetch as fetch_mod
from ghpr.commands import pull as pull_mod
from ghpr.commands import push as push_mod

DESC = 'r#5.md'


def _git_init(tmp_path):
    proc.run('git', 'init', '-q', log=None)
    proc.run('git', 'config', 'user.email', 't@t.com', log=None)
    proc.run('git', 'config', 'user.name', 't', log=None)
    proc.run('git', 'config', 'pr.owner', 'o', log=None)
    proc.run('git', 'config', 'pr.repo', 'r', log=None)
    proc.run('git', 'config', 'pr.number', '5', log=None)
    # `issue` keeps review-thread sync (a PR-only, GraphQL-backed path) out of
    # scope; description + comment reconciliation is what these tests cover.
    proc.run('git', 'config', 'pr.type', 'issue', log=None)


def _seed(body: str) -> None:
    """Create the initial clone state: description at `body`, ref == HEAD."""
    Path(DESC).write_text(f'# [o/r#5] T\n\n{body}\n\n[o/r#5]: https://github.com/o/r/issues/5\n')
    proc.run('git', 'add', DESC, log=None)
    proc.run('git', 'commit', '-q', '-m', 'clone', log=None)
    refs.set_remote_ref('HEAD')


def _mock_remote(monkeypatch, body: str, comments=()):
    """Stub the GitHub reads `fetch` performs, and neutralize the trailing push."""
    monkeypatch.setattr(fetch_mod, 'get_item_metadata', lambda o, r, n, t=None: (
        {'title': 'T', 'body': body, 'url': 'https://github.com/o/r/issues/5'}, 'issue'))
    monkeypatch.setattr(fetch_mod, 'get_item_comments', lambda o, r, n, t: list(comments))
    pushes = []
    monkeypatch.setattr(push_mod, 'push', lambda *a, **kw: pushes.append((a, kw)))
    return pushes


def _body() -> str:
    """The description body as it stands in the working tree."""
    lines = Path(DESC).read_text().split('\n')
    return lines[2]


@dataclass
class State:
    """Everything `pull -n` must leave untouched."""
    head: str
    ref: str | None
    status: tuple[str, ...]
    desc: str

    @staticmethod
    def read() -> 'State':
        return State(
            head=proc.line('git', 'rev-parse', 'HEAD', log=None),
            ref=refs.read_remote_ref(),
            status=tuple(proc.lines('git', 'status', '--porcelain', log=None) or []),
            desc=Path(DESC).read_text(),
        )


class TestRemoteRef:
    def test_set_and_read(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        assert refs.read_remote_ref() is None
        _seed('v0')
        assert refs.read_remote_ref() == proc.line('git', 'rev-parse', 'HEAD', log=None)

    def test_ensure_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        sha = refs.read_remote_ref()
        proc.run('git', 'commit', '-q', '--allow-empty', '-m', 'local', log=None)
        assert refs.ensure_remote_ref() == sha


class TestFetch:
    def test_advances_ref_without_touching_branch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v1')
        before = State.read()

        snap = fetch_mod.fetch(dry_run=False, no_comments=False)

        after = State.read()
        assert (snap.changed, snap.desc_changed) == (True, True)
        # The branch, working tree and index are all exactly as they were...
        assert (after.head, after.status, after.desc) == (before.head, before.status, before.desc)
        # ...only the remote-tracking ref moved, and it holds GitHub's body.
        assert after.ref == snap.sha != before.ref
        assert proc.text('git', 'show', f'{snap.sha}:{DESC}', log=None).split('\n')[2] == 'v1'

    def test_dry_run_leaves_ref_alone(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v1')
        before = State.read()

        snap = fetch_mod.fetch(dry_run=True, no_comments=False)

        assert snap.changed
        assert State.read() == before

    def test_no_remote_change_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v0')
        before = State.read()

        snap = fetch_mod.fetch(dry_run=False, no_comments=False)

        assert not snap.changed
        assert State.read() == before

    def test_leaves_no_worktree_behind(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v1')

        fetch_mod.fetch(dry_run=False, no_comments=False)

        assert proc.lines('git', 'worktree', 'list', '--porcelain', log=None)[0] == \
            f'worktree {tmp_path}'


class TestPullRebase:
    def test_replays_unpushed_local_commit(self, tmp_path, monkeypatch):
        """The regression this whole ref exists for.

        A local commit GitHub hasn't seen must survive a pull that brings in an
        unrelated remote edit. The old overwrite-only pull committed the remote
        body straight over it, reverting the local edit and then pushing the
        reversion.
        """
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('shared\nlocal-only')
        # Local, unpushed: append a line at the end.
        Path(DESC).write_text(Path(DESC).read_text().replace('local-only', 'local-only\nmine'))
        proc.run('git', 'commit', '-q', '-am', 'local edit', log=None)
        local_sha = proc.line('git', 'rev-parse', 'HEAD', log=None)
        # Remote, concurrently: edit the *first* line.
        _mock_remote(monkeypatch, 'shared-edited-remotely\nlocal-only')

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        assert Path(DESC).read_text().split('\n')[2:5] == [
            'shared-edited-remotely', 'local-only', 'mine',
        ]
        # Replayed, not merged: linear history, remote commit is an ancestor.
        assert proc.line('git', 'rev-parse', 'HEAD', log=None) != local_sha
        assert proc.check('git', 'merge-base', '--is-ancestor', refs.read_remote_ref(), 'HEAD', log=None)

    def test_fast_forwards_when_nothing_local(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v1')

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        assert _body() == 'v1'
        assert proc.line('git', 'rev-parse', 'HEAD', log=None) == refs.read_remote_ref()

    def test_aborts_on_dirty_worktree(self, tmp_path, monkeypatch):
        """Uncommitted edits used to be silently overwritten by the remote body."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'uncommitted work'))
        _mock_remote(monkeypatch, 'v1')

        with pytest.raises(SystemExit) as exc:
            pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                          gist_private=None, no_comments=False)

        assert exc.value.code == 1
        assert _body() == 'uncommitted work'

    def test_dirty_worktree_ok_when_remote_unchanged(self, tmp_path, monkeypatch):
        """Nothing to reconcile, so the usual edit → pull → push flow still works."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'uncommitted work'))
        pushes = _mock_remote(monkeypatch, 'v0')

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        assert _body() == 'uncommitted work'
        assert len(pushes) == 1


class TestPullDryRun:
    def test_has_no_side_effects(self, tmp_path, monkeypatch):
        """`pull -n` used to write the remote body into the working tree and
        stage it, so a "dry run" left the description modified and in the index.
        """
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v1')
        before = State.read()

        pull_mod.pull(gist=False, dry_run=True, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        assert State.read() == before

    def test_no_side_effects_with_dirty_worktree(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'uncommitted work'))
        _mock_remote(monkeypatch, 'v1')
        before = State.read()

        pull_mod.pull(gist=False, dry_run=True, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        assert State.read() == before


class TestPullModes:
    def test_merge_keeps_local_commit_as_parent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('shared\nlocal-only')
        Path(DESC).write_text(Path(DESC).read_text().replace('local-only', 'local-only\nmine'))
        proc.run('git', 'commit', '-q', '-am', 'local edit', log=None)
        local_sha = proc.line('git', 'rev-parse', 'HEAD', log=None)
        _mock_remote(monkeypatch, 'shared-edited-remotely\nlocal-only')

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False, mode='merge')

        assert Path(DESC).read_text().split('\n')[2:5] == [
            'shared-edited-remotely', 'local-only', 'mine',
        ]
        assert proc.lines('git', 'rev-list', '--parents', '-n1', 'HEAD', log=None)[0].split()[1:] == \
            [local_sha, refs.read_remote_ref()]

    def test_overwrite_discards_local_commit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'mine'))
        proc.run('git', 'commit', '-q', '-am', 'local edit', log=None)
        _mock_remote(monkeypatch, 'v1')

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False, mode='overwrite')

        assert _body() == 'v1'
        assert proc.line('git', 'rev-parse', 'HEAD', log=None) == refs.read_remote_ref()

    def test_config_supplies_default_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        proc.run('git', 'config', 'ghpr.pullMode', 'overwrite', log=None)
        assert pull_mod._resolve_mode(None) == 'overwrite'
        assert pull_mod._resolve_mode('rebase') == 'rebase'

    def test_rejects_unknown_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        with pytest.raises(SystemExit) as exc:
            pull_mod._resolve_mode('rewrite')
        assert exc.value.code == 1


class _NoGh:
    """Forwards to the real `proc`, but swallows `gh` invocations.

    Narrower than stubbing `proc.run` outright: that patches the shared `utz.proc`
    module, which would also silence the `git update-ref` these tests assert on.
    """
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, *args, **kwargs):
        if args and args[0] == 'gh':
            return None
        return self._real.run(*args, **kwargs)


class TestPushAdvancesRef:
    def _stub_push_deps(self, monkeypatch):
        """Let `push` run its ref bookkeeping without any GitHub traffic."""
        monkeypatch.setattr(push_mod, 'get_item_metadata', lambda *a, **kw: ({}, 'issue'))
        monkeypatch.setattr(push_mod, 'get_item_comments', lambda *a, **kw: [])
        monkeypatch.setattr(push_mod, 'get_current_github_user', lambda: 'ryan-williams')
        monkeypatch.setattr(push_mod, 'proc', _NoGh(proc))

    def test_advances_when_fully_synced(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        base = refs.read_remote_ref()
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'mine'))
        proc.run('git', 'commit', '-q', '-am', 'local edit', log=None)
        self._stub_push_deps(monkeypatch)

        push_mod.push(gist=False, dry_run=False, footer=0, no_footer=True, open_browser=False,
                      images=False, gist_private=None, no_comments=False, force_others=False)

        head = proc.line('git', 'rev-parse', 'HEAD', log=None)
        assert (refs.read_remote_ref(), head != base) == (head, True)

    def test_holds_ref_when_files_uncommitted(self, tmp_path, monkeypatch):
        """A dirty file means HEAD != GitHub after the push, so the base must not
        move — otherwise the next pull would treat the local edit as pushed and
        drop it."""
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        base = refs.read_remote_ref()
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'uncommitted'))
        self._stub_push_deps(monkeypatch)

        push_mod.push(gist=False, dry_run=False, footer=0, no_footer=True, open_browser=False,
                      images=False, gist_private=None, no_comments=False, force_others=False)

        assert refs.read_remote_ref() == base

    def test_holds_ref_when_comments_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        base = refs.read_remote_ref()
        self._stub_push_deps(monkeypatch)

        push_mod.push(gist=False, dry_run=False, footer=0, no_footer=True, open_browser=False,
                      images=False, gist_private=None, no_comments=True, force_others=False)

        assert refs.read_remote_ref() == base

    def test_dry_run_never_advances(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        base = refs.read_remote_ref()
        Path(DESC).write_text(Path(DESC).read_text().replace('v0', 'mine'))
        proc.run('git', 'commit', '-q', '-am', 'local edit', log=None)
        self._stub_push_deps(monkeypatch)
        monkeypatch.setattr(push_mod, 'render_comment_diff', lambda *a, **kw: None)

        push_mod.push(gist=False, dry_run=True, footer=0, no_footer=True, open_browser=False,
                      images=False, gist_private=None, no_comments=False, force_others=False)

        assert refs.read_remote_ref() == base


class TestFetchComments:
    def test_new_remote_comment_lands_in_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v0', comments=[{
            'id': 900, 'user': {'login': 'someone'}, 'body': 'hi there',
            'created_at': '2025-01-01T00:00:00Z', 'updated_at': '2025-01-01T00:00:00Z',
        }])

        snap = fetch_mod.fetch(dry_run=False, no_comments=False)

        assert (snap.new_comments, snap.updated_comments, snap.desc_changed) == (1, 0, False)
        assert proc.lines('git', 'ls-tree', '--name-only', snap.sha, log=None) == [
            DESC, 'z900-someone.md',
        ]

    def test_comments_arrive_via_pull(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        _seed('v0')
        _mock_remote(monkeypatch, 'v0', comments=[{
            'id': 900, 'user': {'login': 'someone'}, 'body': 'hi there',
            'created_at': '2025-01-01T00:00:00Z', 'updated_at': '2025-01-01T00:00:00Z',
        }])

        pull_mod.pull(gist=False, dry_run=False, footer=None, open_browser=False,
                      gist_private=None, no_comments=False)

        _, _, _, body = __import__('ghpr.comments', fromlist=['x']).read_comment_file(
            Path('z900-someone.md'))
        assert body == 'hi there'
