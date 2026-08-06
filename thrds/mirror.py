"""Gist mirror + git version-control for a thrds session.

Session dir layout (ghpr-style, per `gh/{number}/`):

    <parent-git-root-or-cwd>/thrds/<slug>/    session dir; one per doc
      .git/                                   private git repo (nested is fine)
      thrds.json                              thrds state (flat: gists reject dirs)
      <slug>.md                               the doc

The nested `.git/` is invisible to any surrounding project's git — parent's
`git status` shows the session dir as a single untracked entry, no
recursion (git treats a dir containing `.git/` as opaque).

A secret gist created at init becomes the `g` remote. Every state-mutating
verb (`push`, `pull --write`, `archive`) auto-commits + pushes; the gist
carries the full state.json + doc.md edit history for multi-machine sync.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class MirrorError(RuntimeError):
    """Wraps `subprocess.CalledProcessError` with command + stderr for context."""


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``cmd`` (list form, no shell); raise `MirrorError` on non-zero exit.

    stdout / stderr captured. Text mode so stdout/stderr are ``str``.
    """
    try:
        return subprocess.run(
            cmd, cwd=cwd, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise MirrorError(
            f"{' '.join(cmd)} failed (exit {e.returncode}):\n{e.stderr.rstrip()}"
        ) from e


def find_git_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the nearest git repo root; None if none.

    Used to place ``thrds/<slug>/`` under the enclosing project's root when
    one exists (co-located with the work it's for); falls back to ``start``
    otherwise.
    """
    try:
        result = _run(['git', 'rev-parse', '--show-toplevel'], cwd=start)
        return Path(result.stdout.strip())
    except MirrorError:
        return None


def is_git_repo(path: Path) -> bool:
    """True iff ``path`` has a ``.git`` dir (i.e. is a repo top-level)."""
    return (path / '.git').is_dir()


def resolve_session_dir(cwd: Path, doc_slug: str) -> Path:
    """Compute the target session dir: ``<git-root-or-cwd>/thrds/<slug>/``."""
    root = find_git_root(cwd) or cwd
    return root / 'thrds' / doc_slug


def init_repo(session_dir: Path) -> None:
    """``git init -q -b main`` inside ``session_dir``. Idempotent."""
    if not is_git_repo(session_dir):
        _run(['git', 'init', '-q', '-b', 'main'], cwd=session_dir)


def _has_staged_changes(session_dir: Path) -> bool:
    """True iff `git diff --cached` has any hunks."""
    r = _run(['git', 'diff', '--cached', '--stat'], cwd=session_dir)
    return bool(r.stdout.strip())


def commit(session_dir: Path, paths: list[str], message: str) -> str | None:
    """Stage ``paths`` and commit with ``message``; return HEAD SHA, or None
    if nothing was staged (idempotent when files are already committed at
    current content — no empty commits)."""
    _run(['git', 'add', *paths], cwd=session_dir)
    if not _has_staged_changes(session_dir):
        return None
    _run(['git', 'commit', '-q', '-m', message], cwd=session_dir)
    return _run(['git', 'rev-parse', 'HEAD'], cwd=session_dir).stdout.strip()


def has_remote(session_dir: Path, name: str) -> bool:
    """True iff ``session_dir`` has a git remote named ``name``."""
    r = _run(['git', 'remote'], cwd=session_dir)
    return name in r.stdout.split()


def add_remote(session_dir: Path, name: str, url: str) -> None:
    """``git remote add`` (or ``set-url`` if it already exists)."""
    if has_remote(session_dir, name):
        _run(['git', 'remote', 'set-url', name, url], cwd=session_dir)
    else:
        _run(['git', 'remote', 'add', name, url], cwd=session_dir)


def push(session_dir: Path, remote: str = 'g', branch: str = 'main') -> None:
    """``git push <remote> HEAD:<branch>``. No-op if remote isn't configured
    (a `--no-gist` init leaves no ``g`` remote; commits still happen locally)."""
    if not has_remote(session_dir, remote):
        return
    _run(['git', 'push', '-q', remote, f'HEAD:{branch}'], cwd=session_dir)


def align_to_remote(session_dir: Path, remote: str = 'g', branch: str = 'main') -> None:
    """`git fetch <remote>` + `git reset --hard <remote>/<branch>`.

    After `create_gist` populates a gist from a file directly, our local git
    history has no shared ancestor with the gist's initial commit — a
    subsequent `git push` would be rejected as non-fast-forward. Aligning
    here (drop any pre-existing local HEAD, adopt gist HEAD verbatim) lets
    our subsequent state.json commit fast-forward cleanly. Only safe on a
    freshly-init'd session dir where no meaningful commits exist yet.
    """
    _run(['git', 'fetch', '-q', remote], cwd=session_dir)
    _run(['git', 'reset', '-q', '--hard', f'{remote}/{branch}'], cwd=session_dir)


def create_gist(session_dir: Path, description: str, files: list[str]) -> tuple[str, str]:
    """Create a secret gist via ``gh gist create`` and return (gist_id, git_url).

    Depends on the ``gh`` CLI (authenticated). ``gh gist create`` defaults to
    secret (``--public`` to opt out; there is no ``--secret`` flag). Stdout
    is the gist URL — parse out the gist ID and construct the git-clone URL
    so `add_remote` can point ``g`` at it.
    """
    r = _run(
        ['gh', 'gist', 'create', '--desc', description, *files],
        cwd=session_dir,
    )
    # gh writes progress to stderr; the URL is the last non-empty stdout line.
    url_lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    if not url_lines:
        raise MirrorError(f"gh gist create produced no output; stderr: {r.stderr}")
    url = url_lines[-1]
    gist_id = url.rsplit('/', 1)[-1]
    # SSH URL (not HTTPS) — the HTTPS variant would prompt for a username on
    # `git push` unless the user has run `gh auth setup-git`; SSH just works
    # as long as their GH SSH key is set up (the common case).
    git_url = f'git@gist.github.com:{gist_id}.git'
    return gist_id, git_url


def commit_and_push(
    session_dir: Path,
    paths: list[str],
    message: str,
    remote: str = 'g',
) -> str | None:
    """`commit()` then `push()` — the standard "state changed, mirror it" call.

    Returns the new HEAD SHA if a commit happened, else None. If no remote is
    configured, the commit still fires but push is a no-op — local history
    accumulates even without a gist mirror.
    """
    sha = commit(session_dir, paths, message)
    if sha is not None:
        push(session_dir, remote=remote)
    return sha
