"""`refs/remotes/github/remote`: a remote-tracking ref for GitHub's state.

A ghpr item repo has no remote for GitHub itself — the only remote is the gist,
which is a read replica. Without a record of "the state we last knew GitHub to
be in", `pull` has no merge base, so it can only overwrite local files with
remote content, silently discarding local commits GitHub hasn't seen.

This ref is that base: the analog of `origin/main`. It is never checked out; it
points at a commit whose tree mirrors GitHub. `ghpr fetch` advances it, and
`ghpr pull` reconciles the local branch onto it (rebase by default), exactly as
`git fetch` / `git pull --rebase` do.

It lives under `refs/remotes/` because that is one of the few namespaces git
auto-enables reflogs for (`core.logAllRefUpdates` covers `refs/heads/`,
`refs/remotes/`, `refs/notes/` and `HEAD`). Outside them the ref would keep no
record of its own movements — no `github/remote@{1}`, no "when did it move" —
which is half the value of keeping it. It also means `git branch -r` lists it
and `git log github/remote` reads naturally. There is deliberately no
`[remote "github"]` config section: a URL-less remote would break
`git fetch --all`, and nothing here needs one.
"""

from utz import proc, err

REMOTE_REF = 'refs/remotes/github/remote'
LEGACY_REMOTE_REF = 'refs/ghpr/remote'


def _rev_parse(ref: str) -> str | None:
    return proc.line('git', 'rev-parse', '--verify', '-q', ref, err_ok=True, log=None) or None


def read_remote_ref() -> str | None:
    """Resolve the remote-tracking ref, migrating from the legacy name if needed.

    Returns None only when GitHub's state was never recorded — a repo cloned
    before the ref existed. Callers must treat that as "unknown", not as
    "identical to HEAD"; see `commands/fetch.py:resolve_base`.
    """
    if sha := _rev_parse(REMOTE_REF):
        return sha
    if sha := _rev_parse(LEGACY_REMOTE_REF):
        proc.run('git', 'update-ref', REMOTE_REF, sha, log=None)
        proc.run('git', 'update-ref', '-d', LEGACY_REMOTE_REF, log=None)
        err(f"Migrated {LEGACY_REMOTE_REF} → {REMOTE_REF} (gains a reflog)")
        return sha
    return None


def set_remote_ref(commit: str = 'HEAD') -> str:
    """Point the remote-tracking ref at `commit`; returns the resolved SHA."""
    sha = proc.line('git', 'rev-parse', commit, log=None)
    proc.run('git', 'update-ref', REMOTE_REF, sha, log=None)
    return sha
