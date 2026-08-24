"""Pull command - fetch GitHub state, reconcile the local branch, mirror to the gist.

`pull` does **not** write back to GitHub. Sending local state to the item is
`push`'s job, exclusively — the same split git makes, and the reason `push` can
gate on a stale upstream without double-fetching. `ghpr sync` is the round trip.
"""

import webbrowser

from click import Choice
from utz import proc, err
from utz.cli import flag, opt

from ..refs import REMOTE_REF, set_remote_ref
from .fetch import bootstrap_is_ambiguous, build_snapshot, resolve_base, resolve_item

MODES = ('rebase', 'merge', 'overwrite')


def _current_branch() -> str:
    return proc.line('git', 'rev-parse', '--abbrev-ref', 'HEAD', log=None)


def _wt_dirty() -> list[str]:
    """Tracked files with staged or unstaged changes (untracked drafts don't count)."""
    return proc.lines('git', 'status', '--porcelain', '--untracked-files=no', log=None) or []


def _needs_reconcile(sha: str) -> bool:
    """True when the branch isn't already sitting on top of the remote state."""
    return not proc.check('git', 'merge-base', '--is-ancestor', sha, 'HEAD', log=None)


def _resolve_mode(mode: str | None) -> str:
    if not mode:
        mode = proc.line('git', 'config', 'ghpr.pullMode', err_ok=True, log=None) or 'rebase'
    if mode not in MODES:
        err(f"Error: unknown pull mode {mode!r} (expected one of: {', '.join(MODES)})")
        exit(1)
    return mode


def _reconcile(base: str, sha: str, mode: str) -> None:
    """Bring the checked-out branch onto the newly fetched remote state.

    `base` is the previous `github` ref, so `base..HEAD` is exactly the set of
    local commits GitHub hasn't seen — the commits that must survive.
    """
    n_local = int(proc.line('git', 'rev-list', '--count', f'{base}..HEAD', log=None))
    branch = _current_branch()

    if mode == 'overwrite':
        if n_local:
            err(f"⚠ Discarding {n_local} local commit(s) not on GitHub "
                f"(recoverable at {proc.line('git', 'rev-parse', '--short', 'HEAD', log=None)})")
        proc.run('git', 'reset', '--hard', '-q', sha, log=None)
        err("Reset to remote state (--mode overwrite)")
        return

    if dirty := _wt_dirty():
        err(f"Error: {len(dirty)} file(s) with uncommitted changes; "
            f"{mode} needs a clean working tree:")
        for line in dirty:
            err(f"    {line[3:]}")
        err(f"Commit or stash them, then re-run. {REMOTE_REF} is already updated, so")
        err(f"you can also reconcile by hand (e.g. `git rebase --onto {sha[:8]} {base[:8]}`).")
        exit(1)

    if mode == 'merge':
        proc.run('git', 'merge', '--no-edit', sha, log=None)
        err(f"Merged remote state into {branch}")
    else:
        if n_local:
            err(f"Replaying {n_local} local commit(s) onto remote state...")
        proc.run('git', 'rebase', '--onto', sha, base, branch, log=None)
        err(f"Rebased {branch} onto remote state")


def mirror_to_gist(
    owner: str,
    repo: str,
    number: str,
    gist: bool,
    gist_private: bool | None,
) -> None:
    """Bring the gist read replica back in line with HEAD, after a reconcile.

    The gist mirrors local state, so a reconcile that moved HEAD leaves it
    stale. Only ever an update here: creating the first gist is `-g`'s job (or
    `push`'s), since that also implies a footer edit on the GitHub body.
    """
    from .push import sync_to_gist
    from ..files import read_description_from_git

    has_gist = bool(proc.line('git', 'config', 'pr.gist', err_ok=True, log=None))
    if not (gist or has_gist):
        return
    desc_content, _ = read_description_from_git('HEAD')
    sync_to_gist(owner, repo, number, desc_content or '', gist_private=gist_private)


def pull(
    gist: bool,
    dry_run: bool,
    open_browser: bool,
    gist_private: bool | None,
    no_comments: bool,
    mode: str | None = None,
) -> None:
    """Fetch the latest from GitHub and reconcile it into the local branch."""
    mode = _resolve_mode(mode)
    owner, repo, number, item_type = resolve_item()
    base, bootstrap = resolve_base()

    err("Pulling latest from GitHub...")
    snap = build_snapshot(owner, repo, number, item_type, base, no_comments, keep=not dry_run)
    label = snap.item_label

    if bootstrap and bootstrap_is_ambiguous(snap, mode):
        exit(1)

    if dry_run:
        if snap.changed:
            err(f"[DRY-RUN] Would fetch from {label}: {snap.summary()}")
            proc.run('git', '--no-pager', 'diff', base, snap.sha, log=None)
        else:
            err(f"No changes from {label}")
        if _needs_reconcile(snap.sha):
            n_local = int(proc.line('git', 'rev-list', '--count', f'{snap.sha}..HEAD', log=None))
            err(f"[DRY-RUN] Would {mode} {n_local} local commit(s) onto the fetched state")
        return

    if snap.changed:
        set_remote_ref(snap.sha)
        err(f"Fetched from {label}: {snap.summary()}")
    else:
        if bootstrap:
            set_remote_ref(base)
        err(f"No changes from {label}")

    # Reconcile off the ref's position, not off what this fetch happened to see:
    # a `push` gate refusal advances the ref without moving HEAD, so "the fetch
    # found nothing new" and "the branch is up to date" are different questions.
    if not _needs_reconcile(snap.sha):
        return
    # `snap.sha == base` when the fetch found nothing, so this is `--onto base base`:
    # a no-op replay of the same `base..HEAD` commits, which is exactly right.
    _reconcile(base, snap.sha, mode)
    mirror_to_gist(owner, repo, number, gist, gist_private)

    if open_browser:
        item_url = proc.line('git', 'config', 'pr.url', err_ok=True, log=None)
        if not item_url:
            path_part = 'pull' if item_type == 'pr' else 'issues'
            item_url = f'https://github.com/{owner}/{repo}/{path_part}/{number}'
        webbrowser.open(item_url)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @flag('--no-comments', help='Skip syncing comments')
    @opt('-p/-P', '--private/--public', 'gist_private', default=None, help='Gist visibility: -p = private, -P = public (default: match repo visibility)')
    @flag('-o', '--open', 'open_browser', help='Open PR in browser after pulling')
    @opt('-m', '--mode', type=Choice(MODES), default=None, help='Reconcile local commits with fetched state (default: `git config ghpr.pullMode`, else rebase)')
    @flag('-n', '--dry-run', help='Show what would be done')
    @flag('-g', '--gist', help='Create the gist mirror if it does not exist yet')
    def pull_cmd(no_comments, gist_private, open_browser, mode, dry_run, gist):
        """Fetch GitHub state and reconcile it into the local branch (does not push)."""
        pull(gist, dry_run, open_browser, gist_private, no_comments, mode)
