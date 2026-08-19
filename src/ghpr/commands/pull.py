"""Pull command - fetch GitHub state, reconcile the local branch, push back."""

from click import Choice
from utz import proc, err
from utz.cli import flag, opt

from ..refs import ensure_remote_ref, set_remote_ref
from .fetch import build_snapshot, resolve_item

MODES = ('rebase', 'merge', 'overwrite')


def _current_branch() -> str:
    return proc.line('git', 'rev-parse', '--abbrev-ref', 'HEAD', log=None)


def _wt_dirty() -> list[str]:
    """Tracked files with staged or unstaged changes (untracked drafts don't count)."""
    return proc.lines('git', 'status', '--porcelain', '--untracked-files=no', log=None) or []


def _resolve_mode(mode: str | None) -> str:
    if not mode:
        mode = proc.line('git', 'config', 'ghpr.pullMode', err_ok=True, log=None) or 'rebase'
    if mode not in MODES:
        err(f"Error: unknown pull mode {mode!r} (expected one of: {', '.join(MODES)})")
        exit(1)
    return mode


def _reconcile(base: str, sha: str, mode: str) -> None:
    """Bring the checked-out branch onto the newly fetched remote state.

    `base` is the previous `refs/ghpr/remote`, so `base..HEAD` is exactly the set
    of local commits GitHub hasn't seen — the commits that must survive.
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
        err("Commit or stash them, then re-run. `refs/ghpr/remote` is already updated, so")
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


def pull(
    gist: bool,
    dry_run: bool,
    footer: bool | None,
    open_browser: bool,
    gist_private: bool | None,
    no_comments: bool,
    mode: str | None = None,
) -> None:
    """Fetch the latest from GitHub, reconcile locally, then push back."""
    # Import push here to avoid circular dependency
    from . import push as push_module

    mode = _resolve_mode(mode)
    owner, repo, number, item_type = resolve_item()
    base = ensure_remote_ref()

    err("Pulling latest from GitHub...")
    snap = build_snapshot(owner, repo, number, item_type, base, no_comments, keep=not dry_run)
    label = snap.item_label

    if not snap.changed:
        err(f"No changes from {label}")
    elif dry_run:
        err(f"[DRY-RUN] Would fetch from {label}: {snap.summary()}")
        proc.run('git', '--no-pager', 'diff', base, snap.sha, log=None)
        n_local = int(proc.line('git', 'rev-list', '--count', f'{base}..HEAD', log=None))
        err(f"[DRY-RUN] Would {mode} {n_local} local commit(s) onto the fetched state")
    else:
        set_remote_ref(snap.sha)
        err(f"Fetched from {label}: {snap.summary()}")
        _reconcile(base, snap.sha, mode)

    # Now push our version back
    err(f"Pushing to {label}...")
    # Convert pull's footer boolean to push's footer count
    footer_count = 1 if footer else 0 if footer is False else 0
    push_module.push(gist, dry_run, footer_count, no_footer=False, open_browser=open_browser, images=False, gist_private=gist_private, no_comments=no_comments, force_others=False)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @flag('--no-comments', help='Skip syncing comments')
    @opt('-p/-P', '--private/--public', 'gist_private', default=None, help='Gist visibility: -p = private, -P = public (default: match repo visibility)')
    @flag('-o', '--open', 'open_browser', help='Open PR in browser after pulling')
    @opt('-m', '--mode', type=Choice(MODES), default=None, help='Reconcile local commits with fetched state (default: `git config ghpr.pullMode`, else rebase)')
    @opt('-f/-F', '--footer/--no-footer', default=None, help='Add gist footer to PR (default: auto - add if gist exists)')
    @flag('-n', '--dry-run', help='Show what would be done')
    @flag('-g', '--gist', help='Also sync to gist')
    def pull_cmd(no_comments, gist_private, open_browser, mode, footer, dry_run, gist):
        """Pull latest PR/Issue description and comments from GitHub."""
        pull(gist, dry_run, footer, open_browser, gist_private, no_comments, mode)
