"""`refs/remotes/github`: a remote-tracking ref for GitHub's state.

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
record of its own movements — no `github@{1}`, no "when did it move" — which is
half the value of keeping it. It also means `git branch -r` lists it and
`git log github` reads naturally. There is deliberately no `[remote "github"]`
config section: a URL-less remote would break `git fetch --all`, and nothing
here needs one.

The name is the *remote's* name and nothing else, matching git's own
`refs/remotes/<remote>/<branch>` shape with the branch component elided (there
is only one state per item, so there is no branch to name). The earlier
`github/remote` said "remote" twice; `git diff github HEAD` reads as intended.
thrds converged on the same shape (`refs/remotes/{staging,prod}`).
"""

from utz import proc, err

REMOTE_REF = 'refs/remotes/github'
# Tried in order; each migrates to `REMOTE_REF` on first read.
LEGACY_REMOTE_REFS = ('refs/remotes/github/remote', 'refs/ghpr/remote')


def _rev_parse(ref: str) -> str | None:
    return proc.line('git', 'rev-parse', '--verify', '-q', ref, err_ok=True, log=None) or None


def read_remote_ref() -> str | None:
    """Resolve the remote-tracking ref, migrating from a legacy name if needed.

    Returns None only when GitHub's state was never recorded — a repo cloned
    before the ref existed. Callers must treat that as "unknown", not as
    "identical to HEAD"; see `commands/fetch.py:resolve_base`.
    """
    if sha := _rev_parse(REMOTE_REF):
        return sha
    for legacy in LEGACY_REMOTE_REFS:
        if not (sha := _rev_parse(legacy)):
            continue
        # Delete first: `refs/remotes/github/remote` and `refs/remotes/github`
        # are a directory/file conflict, so the new ref cannot be created while
        # the old one exists.
        proc.run('git', 'update-ref', '-d', legacy, log=None)
        proc.run('git', 'update-ref', REMOTE_REF, sha, log=None)
        err(f"Migrated {legacy} → {REMOTE_REF}")
        return sha
    return None


def set_remote_ref(commit: str = 'HEAD') -> str:
    """Point the remote-tracking ref at `commit`; returns the resolved SHA."""
    sha = proc.line('git', 'rev-parse', commit, log=None)
    proc.run('git', 'update-ref', REMOTE_REF, sha, log=None)
    return sha
