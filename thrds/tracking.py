"""Remote-tracking refs for pseudo-remotes: our record of what Slack last said.

A Slack channel is mutable shared state with no version tracking, no
compare-and-swap, and no way to ask "did this change since I last looked?"
other than reading it. A thrds session dir is already a git repo, so the
missing half can live locally: one ref per pseudo-remote, pointing at a commit
whose tree is what we last observed. `origin/main`, for a remote that can't
keep one itself.

Three refs, because a session's content comes from two places::

    refs/remotes/staging     what the staging PC says (draft threads)
    refs/remotes/prod        what each target says (posted threads)
    refs/heads/upstream      their union — the merge base

`upstream` is the only one to reconcile against. Drafts exist only in staging,
and a posted thread is canonical in prod (its staged copy is frozen), so each
source tree is partial and rebasing onto one would delete the other's threads.
The sources are near-free — `pull` already makes both API calls separately —
and they answer the question the composite can't: *has the frozen staged copy
drifted from what's live?*, one `git diff staging prod` away.

**Why these namespaces** (converged with ghpr; each session's earlier
`refs/remotes/<platform>/{staging,prod,remote}` layout migrates on first
touch, see :func:`migrate_refs`). git's own shape puts the *remote's name*
first — `refs/remotes/<remote>/…` — so per-remote refs live at
`refs/remotes/<name>` and `git branch -r` reads as exactly the remote list
(`staging`, `prod`). The composite is *not* a remote — it's the locally
computed upstream projection — so its honest home is a branch:
`refs/heads/upstream`, never checked out. Both namespaces get automatic
reflogs (git auto-enables them for `refs/heads`, `refs/remotes`, `refs/notes`
and `HEAD`); each snapshot is parented on the previous one, so
`git log upstream` is the history of remote states either way — the reflog
adds `@{1}` addressing, movement timestamps, and gc protection should that
chain ever be broken. `git remote` won't list these: there is deliberately no
config section, since a URL-less remote would break `git fetch --all`.

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
COMPOSITE = 'upstream'

# The pre-named-remotes layout: platform-prefixed sources, composite named
# `remote` (and living under refs/remotes/, which it isn't one of).
LEGACY_COMPOSITE = 'remote'


def ref_name(name: str) -> str:
    """The full ref for a tracking name: ``refs/remotes/<name>`` for a remote,
    ``refs/heads/upstream`` for the composite (a computed branch, not a
    remote). The short name resolves either way (`git diff staging upstream`),
    so ``name`` doubles as the display label."""
    if name == COMPOSITE:
        return f'refs/heads/{COMPOSITE}'
    return f'refs/remotes/{name}'


def base_ref_name(name: str) -> str:
    """The full ref for a remote's *gate base*: ``refs/heads/base/<name>``.

    Distinct from :func:`ref_name`, and the distinction is the whole point.
    ``refs/remotes/<name>`` records what we last **observed** at the remote and
    advances on every look, including a look that ends in a refusal — that's
    what keeps `git diff <name> HEAD` readable after one. This ref records the
    remote state HEAD has **incorporated**, and advances only when that becomes
    true: a reconcile, a successful push, or an observation that comes back
    already matching HEAD (nothing to incorporate).

    Splitting them is what stops `push`'s gate from disarming itself. A gate
    asking "did anything change since I last looked?" writes its own answer
    when it refuses, so the retry sees nothing new and overwrites the very edit
    the refusal protected. Asking "does HEAD contain what the remote holds
    now?" has no term that the act of asking moves, so it survives being asked
    twice and clears only when someone actually reconciles. Full trace and the
    ghpr-side history in `specs/push-gate-ancestry.md`.

    Under ``refs/heads/`` for the reflog (git auto-enables it there, not under
    an invented namespace), as a computed pointer that is never checked out —
    the same footing as the ``upstream`` composite.
    """
    return f'refs/heads/base/{name}'


def set_base(session_dir: Path, name: str, sha: str) -> None:
    """Record ``sha`` as the remote state HEAD incorporates. See
    :func:`base_ref_name` for when a caller is entitled to do this."""
    _git(session_dir, 'update-ref', base_ref_name(name), sha)


SEEDED_KEY = 'thrds.baseRefsSeeded'


def seed_base_refs(session_dir: Path, names: list[str]) -> list[str]:
    """One-time: adopt each remote's observation ref as its gate base.

    A session fetched before base refs existed has observations but no record
    of what HEAD incorporated. Left unset, the *first* push after the upgrade
    would find no base and sail through as a bootstrap — unguarded, which is
    strictly worse than the behavior being replaced (that one gated against the
    observation). Seeding from the observation restores exactly the old
    protection, and is safe in the same way the old gate was.

    Guarded by a config flag rather than by "is the base missing?", because
    missing is *also* how a legitimately diverged fetch leaves things: re-seeding
    on every command would hand the gate its own answer again, which is the bug
    the base ref exists to prevent. Runs once per session, then never again.

    Returns the names seeded (empty on every call after the first).
    """
    if config_get(session_dir, SEEDED_KEY) is not None:
        return []
    seeded = []
    for name in names:
        if read_ref(session_dir, base_ref_name(name)) is not None:
            continue
        if (sha := read_ref(session_dir, ref_name(name))) is not None:
            set_base(session_dir, name, sha)
            seeded.append(name)
    _git(session_dir, 'config', SEEDED_KEY, '1')
    return seeded


def migrate_refs(session_dir: Path, platform: str) -> list[tuple[str, str]]:
    """Move a session's refs from the legacy layout to the current one.

    ``refs/remotes/<platform>/{staging,prod}`` → ``refs/remotes/{staging,prod}``
    and ``refs/remotes/<platform>/remote`` → ``refs/heads/upstream``. Returns
    the ``(old, new)`` pairs actually moved, for the caller to report. If a
    ref somehow exists in both layouts the new one is kept (it's the one
    current code has been advancing) and the stale legacy ref is dropped.
    """
    pairs = [
        (f'refs/remotes/{platform}/{STAGING}', ref_name(STAGING)),
        (f'refs/remotes/{platform}/{PROD}', ref_name(PROD)),
        (f'refs/remotes/{platform}/{LEGACY_COMPOSITE}', ref_name(COMPOSITE)),
    ]
    moved = []
    for old, new in pairs:
        sha = read_ref(session_dir, old)
        if sha is None:
            continue
        if read_ref(session_dir, new) is None:
            _git(session_dir, 'update-ref', new, sha)
            moved.append((old, new))
        _git(session_dir, 'update-ref', '-d', old)
    return moved


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

    Used for the *source* refs, which are observations: `prod` holds the
    posted threads and nothing else, because that is all the target channel
    has. Being sparse in different ways makes a plain `git diff staging
    prod` report every draft as "deleted from prod", so the drift
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
    `thrds.yml`, downloaded `emoji-*.png` — none of which Slack has any
    opinion about. A merge base whose tree omitted them would read as "HEAD
    has four files the remote doesn't", and, far worse, a later
    `git rebase --onto upstream` would *delete* them: a rebase replays
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
