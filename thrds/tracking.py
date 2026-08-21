"""Remote-tracking refs for pseudo-remotes: our record of what Slack last said.

A Slack channel is mutable shared state with no version tracking, no
compare-and-swap, and no way to ask "did this change since I last looked?"
other than reading it. A thrds session dir is already a git repo, so the
missing half can live locally: one ref per pseudo-remote, pointing at a commit
whose tree is what we last observed. `origin/main`, for a remote that can't
keep one itself.

Three refs, because a session's content comes from two places::

    refs/remotes/slack/staging     what the staging PC says (draft threads)
    refs/remotes/slack/prod        what each target says (posted threads)
    refs/remotes/slack/remote      their union — the merge base

`remote` is the only one to reconcile against. Drafts exist only in staging,
and a posted thread is canonical in prod (its staged copy is frozen), so each
source tree is partial and rebasing onto one would delete the other's threads.
The sources are near-free — `pull` already makes both API calls separately —
and they answer the question the composite can't: *has the frozen staged copy
drifted from what's live?*, one `git diff slack/staging slack/prod` away.

**Why `refs/remotes/` and not `refs/thrds/`.** git auto-enables reflogs only
for `refs/heads`, `refs/remotes`, `refs/notes` and `HEAD`. Each snapshot is
parented on the previous one, so `git log slack/remote` is the history of
remote states either way; what the reflog adds is `@{1}` addressing, movement
timestamps, and gc protection should that chain ever be broken. The namespace
also buys `git branch -r` and `git diff slack/staging HEAD`, each reading as
precisely what it is. `git remote` won't list `slack`: there is deliberately
no config section for it, since a URL-less remote would break
`git fetch --all`.

**Why plumbing and not a scratch worktree.** Snapshots are built with
`hash-object` / `mktree` / `commit-tree`, so nothing outside the object
database and the ref is ever touched. A dry run is then side-effect-free by
construction rather than by remembering to honour a flag — which is the bug
this design exists to avoid — and there's no scratch worktree to leak if we
die mid-fetch.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .mirror import MirrorError

# Sources, and the composite that unions them.
STAGING = 'staging'
PROD = 'prod'
COMPOSITE = 'remote'


def ref_name(platform: str, source: str) -> str:
    """The ref for one pseudo-remote, e.g. ``refs/remotes/slack/staging``."""
    return f'refs/remotes/{platform}/{source}'


def short_ref(platform: str, source: str) -> str:
    """How git would print it, e.g. ``slack/staging``."""
    return f'{platform}/{source}'


def _git(
    session_dir: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    try:
        r = subprocess.run(
            ['git', *args], cwd=session_dir, input=stdin,
            env={**os.environ, **env} if env else None,
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise MirrorError(
            f"git {' '.join(args)} failed (exit {e.returncode}):\n{e.stderr.rstrip()}"
        ) from e
    return r.stdout.strip()


def _rev(session_dir: Path, rev: str) -> str | None:
    """Resolve ``rev``, or None if it doesn't exist."""
    r = subprocess.run(
        ['git', 'rev-parse', '--verify', '-q', rev],
        cwd=session_dir, capture_output=True, text=True,
    )
    return r.stdout.strip() or None


def read_ref(session_dir: Path, ref: str) -> str | None:
    """The commit a tracking ref points at, or None if it's never been set."""
    return _rev(session_dir, ref)


def empty_tree(session_dir: Path) -> str:
    """This repo's empty-tree hash (differs between sha1 and sha256 repos)."""
    return _git(session_dir, 'mktree', stdin='')


def _blobs(session_dir: Path, files: dict[str, str]) -> dict[str, str]:
    return {
        name: _git(session_dir, 'hash-object', '-w', '--stdin', stdin=text)
        for name, text in files.items()
    }


def build_tree(session_dir: Path, files: dict[str, str]) -> str:
    """Hash ``{filename: text}`` into a flat tree of exactly those files.

    Flat because gists are flat, so a session dir has no subdirectories to
    mirror. `mktree` sorts its input itself.

    Used for the *source* refs, which are observations: `slack/prod` holds the
    posted threads and nothing else, because that is all the target channel
    has. Being sparse in different ways makes a plain `git diff slack/staging
    slack/prod` report every draft as "deleted from prod", so the drift
    question — *has the frozen staged copy diverged from what's live?* — is
    asked with ``--diff-filter=M``, which keeps only files present in both.
    """
    if not files:
        return empty_tree(session_dir)
    entries = [
        f'100644 blob {blob}\t{name}'
        for name, blob in _blobs(session_dir, files).items()
    ]
    return _git(session_dir, 'mktree', stdin='\n'.join(entries) + '\n')


def overlay_tree(
    session_dir: Path,
    base_tree: str,
    files: dict[str, str],
    prune: set[str],
) -> str:
    """``base_tree`` with ``files`` written over it and ``prune`` removed.

    What the *composite* ref needs, and the reason it can't be a bare
    :func:`build_tree`. A session dir holds more than threads — `README.md`,
    `thrds.json`, downloaded `emoji-*.png` — none of which Slack has any
    opinion about. A merge base whose tree omitted them would read as "HEAD
    has four files the remote doesn't", and, far worse, a later
    `git rebase --onto slack/remote` would *delete* them: a rebase replays
    commits onto the new tree, so whatever isn't there is gone.

    So the composite is "what the tree would look like after `pull`": HEAD's
    tree, with the threads Slack knows about replaced, and the ones it has
    dropped removed. ``prune`` is the set of names this remote is authoritative
    for — thread files — so an unrelated file is never a candidate for removal.

    Built through a scratch index rather than the real one, so the working tree
    and `git status` stay untouched.
    """
    index = Path(_git(session_dir, 'rev-parse', '--git-dir')) / 'thrds-fetch-index'
    if not index.is_absolute():
        index = session_dir / index
    index.unlink(missing_ok=True)
    try:
        env = {'GIT_INDEX_FILE': str(index)}
        _git(session_dir, 'read-tree', base_tree, env=env)
        for name in sorted(prune - set(files)):
            _git(session_dir, 'update-index', '--force-remove', name, env=env)
        for name, blob in _blobs(session_dir, files).items():
            _git(
                session_dir, 'update-index', '--add',
                '--cacheinfo', f'100644,{blob},{name}', env=env,
            )
        return _git(session_dir, 'write-tree', env=env)
    finally:
        index.unlink(missing_ok=True)


def tree_names(session_dir: Path, tree: str) -> tuple[str, ...]:
    """Filenames in ``tree`` (flat, so no recursion needed)."""
    out = _git(session_dir, 'ls-tree', '--name-only', tree)
    return tuple(out.splitlines()) if out else ()


def tree_files(session_dir: Path, rev: str) -> dict[str, str]:
    """``{name: content}`` for every file in ``rev``'s (flat) tree."""
    return {
        name: read_tree_file(session_dir, rev, name)
        for name in tree_names(session_dir, rev)
    }


def config_get(session_dir: Path, key: str) -> str | None:
    """``git config <key>``, or None when unset."""
    r = subprocess.run(
        ['git', 'config', key], cwd=session_dir, capture_output=True, text=True,
    )
    return r.stdout.strip() or None


@dataclass(frozen=True)
class MergeResult:
    """`git merge-tree --write-tree` output: the merged tree, and what conflicted.

    ``tree`` is valid even when there are conflicts (it contains conflict
    markers) — callers must check ``conflicts`` before using it, because
    writing marker-bearing content to a thread file would get *pushed to
    Slack* by a later sync.
    """
    tree: str
    conflicts: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.conflicts


def merge_trees(session_dir: Path, base: str, ours: str, theirs: str) -> MergeResult:
    """Three-way merge entirely in the object database; nothing else touched.

    The reconcile primitive for `pull`: ``base`` is the last-fetched remote
    state, ``ours`` is HEAD, ``theirs`` the fresh fetch. A real `git rebase`
    would replay ``base..HEAD`` — but the composite tree overlays HEAD, so
    those commits' non-thread changes are already present and patch-id
    skipping makes replay fragile rather than wrong. `merge-tree` computes
    the same result directly.
    """
    r = subprocess.run(
        ['git', 'merge-tree', '--write-tree', '--name-only',
         f'--merge-base={base}', ours, theirs],
        cwd=session_dir, capture_output=True, text=True,
    )
    lines = r.stdout.splitlines()
    if r.returncode == 0:
        return MergeResult(tree=lines[0].strip(), conflicts=())
    if r.returncode == 1:
        # Line 1 is the (marker-bearing) tree; then conflicted filenames up to
        # the first blank line; then informational messages.
        names = []
        for line in lines[1:]:
            if not line.strip():
                break
            names.append(line.strip())
        return MergeResult(tree=lines[0].strip(), conflicts=tuple(dict.fromkeys(names)))
    raise MirrorError(
        f"git merge-tree failed (exit {r.returncode}):\n{r.stderr.rstrip()}"
    )


def read_tree_file(session_dir: Path, rev: str, name: str) -> str:
    """``name``'s exact content at ``rev`` — no stripping, unlike :func:`_git`,
    because a trailing newline is part of the bytes being compared."""
    try:
        r = subprocess.run(
            ['git', 'show', f'{rev}:{name}'],
            cwd=session_dir, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise MirrorError(
            f"git show {rev}:{name} failed (exit {e.returncode}):\n{e.stderr.rstrip()}"
        ) from e
    return r.stdout


def changed_paths(session_dir: Path, before: str, after: str) -> tuple[str, ...]:
    """Paths differing between two trees or commits."""
    out = _git(session_dir, 'diff', '--name-only', before, after)
    return tuple(out.splitlines()) if out else ()


@dataclass(frozen=True)
class Snapshot:
    """One fetch's result for one ref.

    ``base`` is where the ref pointed before (None on a first fetch), ``sha``
    where it points after. They're equal exactly when Slack projected
    byte-identically to what we already had.
    """
    ref: str
    label: str
    base: str | None
    sha: str
    tree: str
    paths: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.sha != self.base

    def summary(self) -> str:
        n = len(self.paths)
        if self.base is None:
            # A first fetch has nothing to have changed *from*, so every file
            # is "new" and listing them says nothing. Note that the ref now
            # exists — including when what it records is that Slack has
            # nothing, which is a different fact from "nothing changed".
            return f'initialized ({self._n_files})' if n else 'initialized empty'
        if not n:
            return 'up to date'
        return f"{self._n_files} ({', '.join(self.paths)})"

    @property
    def _n_files(self) -> str:
        n = len(self.paths)
        return f"{n} file{'' if n == 1 else 's'}"


def snapshot(
    session_dir: Path,
    ref: str,
    label: str,
    tree: str,
    message: str,
    write: bool = True,
) -> Snapshot:
    """Record ``tree`` as this ref's remote state; advance the ref if it moved.

    A nop when the projected tree matches what the ref already holds — which
    is what makes "a fetch right after a push changes nothing" a property
    `push` can gate on rather than a hope.

    The comparison is content equality, not a version token: Slack offers no
    such token, so a remote change that projects to identical markdown (an
    edit reverted before we looked, whitespace Slack normalizes away) reads as
    no change. That's the right unit — content is what we sync — but it isn't
    the same guarantee a real remote gives.

    On a first fetch the snapshot is parented on `HEAD` rather than assuming
    `HEAD` already *is* remote state the way a bootstrap must when it can't
    look. We can look, so the ref starts out honest and `<ref>..HEAD` is
    correctly empty.

    ``write=False`` still creates the commit object — unreferenced, so it
    costs a few bytes until gc — because a dry run has to be able to show the
    diff it would have applied.
    """
    base = read_ref(session_dir, ref)
    base_tree = f'{base}^{{tree}}' if base is not None else empty_tree(session_dir)
    paths = changed_paths(session_dir, base_tree, tree)

    if base is not None and not paths:
        return Snapshot(ref=ref, label=label, base=base, sha=base, tree=tree, paths=())

    parent = base if base is not None else _rev(session_dir, 'HEAD')
    args = ['commit-tree', tree, '-m', message]
    if parent is not None:
        args += ['-p', parent]
    sha = _git(session_dir, *args)
    if write:
        _git(session_dir, 'update-ref', ref, sha)
    return Snapshot(ref=ref, label=label, base=base, sha=sha, tree=tree, paths=paths)
