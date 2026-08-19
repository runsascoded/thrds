"""`refs/ghpr/remote`: a remote-tracking ref for GitHub's state.

A ghpr item repo has no remote for GitHub itself — the only remote is the gist,
which is a read replica. Without a record of "the state we last knew GitHub to
be in", `pull` has no merge base, so it can only overwrite local files with
remote content, silently discarding local commits GitHub hasn't seen.

`refs/ghpr/remote` is that base: the analog of `origin/main`. It is never
checked out; it points at a commit whose tree mirrors GitHub. `ghpr fetch`
advances it, and `ghpr pull` reconciles the local branch onto it (rebase by
default), exactly as `git fetch` / `git pull --rebase` do.
"""

from utz import proc, err

REMOTE_REF = 'refs/ghpr/remote'


def read_remote_ref() -> str | None:
    """Resolve `refs/ghpr/remote`, or None if the repo predates it."""
    sha = proc.line('git', 'rev-parse', '--verify', '-q', REMOTE_REF, err_ok=True, log=None)
    return sha or None


def set_remote_ref(commit: str = 'HEAD') -> str:
    """Point `refs/ghpr/remote` at `commit`; returns the resolved SHA."""
    sha = proc.line('git', 'rev-parse', commit, log=None)
    proc.run('git', 'update-ref', REMOTE_REF, sha, log=None)
    return sha


def ensure_remote_ref() -> str:
    """Initialize `refs/ghpr/remote` to HEAD if unset (repos cloned pre-`fetch`).

    HEAD is the best available guess at GitHub's state for a legacy repo: the
    true base wasn't recorded, so we assume local == remote. A first `pull` then
    has nothing to replay and degrades to the old remote-wins behavior; every
    subsequent one has a real base.
    """
    if sha := read_remote_ref():
        return sha
    sha = set_remote_ref('HEAD')
    err(f"Initialized {REMOTE_REF} at HEAD ({sha[:8]}), assuming it matches GitHub.")
    err("If HEAD has commits GitHub hasn't seen, point it at the last synced commit first:")
    err(f"    git update-ref {REMOTE_REF} <sha>")
    return sha
