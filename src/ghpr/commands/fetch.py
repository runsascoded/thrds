"""Fetch command - snapshot GitHub state into `refs/remotes/github/remote`.

`fetch` is the read-only half of `pull`: it materializes GitHub's current
description / comments / review threads as a commit and advances
the remote-tracking ref to it, without touching the working tree, the index, or the
checked-out branch. Reconciling (rebase/merge) is `pull`'s job, or the user's.
"""

from dataclasses import dataclass
from glob import glob
from pathlib import Path
from shutil import copytree, rmtree

from utz import proc, err, cd
from utz.cli import flag

from ..api import get_item_metadata, get_item_comments
from ..comments import write_comment_file, read_comment_file, get_comment_id_from_filename
from ..config import get_pr_info_from_path
from ..files import get_expected_description_filename, write_description_with_link_ref
from ..gist import extract_gist_footer
from ..refs import REMOTE_REF, read_remote_ref, set_remote_ref


@dataclass
class Snapshot:
    """GitHub's state as of a fetch, as a commit (`sha`) built on top of `base`."""
    base: str
    sha: str
    item_type: str
    desc_changed: bool = False
    new_comments: int = 0
    updated_comments: int = 0
    threads: int = 0
    review_new: int = 0
    review_updated: int = 0

    @property
    def changed(self) -> bool:
        return self.sha != self.base

    @property
    def item_label(self) -> str:
        return 'issue' if self.item_type == 'issue' else 'PR'

    def summary(self) -> str:
        """Human-readable list of what the remote changed."""
        parts = []
        if self.desc_changed:
            parts.append('description')
        if self.new_comments:
            parts.append(f'{self.new_comments} new comment(s)')
        if self.updated_comments:
            parts.append(f'{self.updated_comments} updated comment(s)')
        if self.review_new or self.review_updated:
            parts.append(f'{self.threads} review thread(s), '
                         f'{self.review_new} new + {self.review_updated} updated review comment(s)')
        return ', '.join(parts)


def resolve_item() -> tuple[str, str, str, str]:
    """Resolve (owner, repo, number, item_type) for the item repo we're in."""
    owner, repo, number = get_pr_info_from_path()
    if not all([owner, repo, number]):
        owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''
        repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None) or ''
        number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None) or ''
        if not all([owner, repo, number]):
            err("Error: Could not determine PR/Issue")
            exit(1)
    item_type = proc.line('git', 'config', 'pr.type', err_ok=True, log=None)
    if not item_type:
        _, item_type = get_item_metadata(owner, repo, number)
    return owner, repo, number, item_type


def _write_remote_state(
    owner: str,
    repo: str,
    number: str,
    item_type: str,
    no_comments: bool,
) -> dict:
    """Overwrite cwd's ghpr files with GitHub's current state; stage the result.

    Only ever called inside the scratch worktree, so overwriting is safe here —
    that isolation is what lets `pull` reconcile with a real rebase instead of
    clobbering the user's files.
    """
    item_data, _ = get_item_metadata(owner, repo, number, item_type)
    if not item_data:
        exit(1)

    desc_file = Path(get_expected_description_filename(owner, repo, number))
    body_without_footer, _ = extract_gist_footer(item_data['body'] or '')
    write_description_with_link_ref(
        desc_file, owner, repo, number,
        item_data['title'], body_without_footer, item_data['url'],
    )
    stats = {'desc_changed': not proc.check('git', 'diff', '--exit-code', str(desc_file), log=None)}
    if stats['desc_changed']:
        proc.run('git', 'add', str(desc_file), log=None)

    new_comments = updated_comments = 0
    if not no_comments:
        remote_comments = get_item_comments(owner, repo, number, item_type)
        existing = {
            cid: f for f in glob('z[0-9]*.md')
            if (cid := get_comment_id_from_filename(f))
        }
        for comment in remote_comments or []:
            comment_id = str(comment['id'])
            author = comment['user']['login']
            body = comment.get('body', '')
            if comment_id in existing:
                old_file = existing[comment_id]
                _, _, _, local_body = read_comment_file(Path(old_file))
                if local_body == body:
                    continue
                new_file = write_comment_file(
                    comment_id, author, comment['created_at'], comment.get('updated_at'), body)
                if str(new_file) != old_file:
                    # Legacy z{id}.md -> z{id}-{author}.md
                    proc.run('git', 'rm', '-q', old_file, log=None)
                proc.run('git', 'add', str(new_file), log=None)
                updated_comments += 1
            else:
                new_file = write_comment_file(
                    comment_id, author, comment['created_at'], comment.get('updated_at'), body)
                proc.run('git', 'add', str(new_file), log=None)
                new_comments += 1
    stats['new_comments'] = new_comments
    stats['updated_comments'] = updated_comments

    threads = review_new = review_updated = 0
    if not no_comments and item_type == 'pr':
        from .. import reviews
        threads, review_new, review_updated = reviews.pull(owner, repo, number)
    stats['threads'] = threads
    stats['review_new'] = review_new
    stats['review_updated'] = review_updated
    return stats


def build_snapshot(
    owner: str,
    repo: str,
    number: str,
    item_type: str,
    base: str,
    no_comments: bool,
    keep: bool,
) -> Snapshot:
    """Build a commit mirroring GitHub, on top of `base`, in a scratch worktree.

    The worktree lives under the git dir and is removed on the way out, so the
    user's working tree and index are untouched — that's what makes `fetch`
    (and `pull -n`) side-effect-free apart from the ref it advances.

    `keep=False` (dry-run) additionally discards the review-thread baselines
    written during the fetch, which would otherwise mask a later resolve flip.
    """
    common = Path(proc.line('git', 'rev-parse', '--git-common-dir', log=None)).resolve()
    wt = common / 'ghpr' / 'fetch-wt'
    _remove_worktree(wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    proc.run('git', 'worktree', 'add', '--detach', '-q', str(wt), base, log=None)
    try:
        with cd(wt):
            stats = _write_remote_state(owner, repo, number, item_type, no_comments)
            snap = Snapshot(base=base, sha=base, item_type=item_type, **stats)
            if not proc.check('git', 'diff', '--cached', '--quiet', log=None):
                label = snap.item_label
                proc.run('git', 'commit', '-q', '-m',
                         f'Fetch from {label}: {snap.summary()}', log=None)
                snap.sha = proc.line('git', 'rev-parse', 'HEAD', log=None)
            if keep:
                _keep_baselines(common)
        return snap
    finally:
        _remove_worktree(wt)


def _remove_worktree(wt: Path) -> None:
    proc.check('git', 'worktree', 'remove', '--force', str(wt), log=None)
    rmtree(wt, ignore_errors=True)
    proc.check('git', 'worktree', 'prune', log=None)


def _keep_baselines(common: Path) -> None:
    """Copy review baselines out of the scratch worktree's per-worktree git dir.

    `reviews.write_baseline` keys off `git rev-parse --git-dir`, which in a
    linked worktree is that worktree's admin dir — so baselines written during a
    fetch are naturally scoped to it, and only promoted when the fetch is kept.
    """
    src = Path(proc.line('git', 'rev-parse', '--git-dir', log=None)) / 'ghpr' / 'reviews'
    if src.is_dir():
        copytree(src, common / 'ghpr' / 'reviews', dirs_exist_ok=True)


def resolve_base() -> tuple[str, bool]:
    """Return (base, is_bootstrap) for the fetch about to run.

    A repo cloned before the ref existed has no recorded base. HEAD is the only
    candidate, but it's a *guess*: adopting it asserts GitHub has already seen
    everything in HEAD. `bootstrap_is_ambiguous` decides whether that guess
    survives contact with what the fetch actually found.
    """
    if sha := read_remote_ref():
        return sha, False
    return proc.line('git', 'rev-parse', 'HEAD', log=None), True


def bootstrap_is_ambiguous(snap: Snapshot, mode: str | None = None) -> bool:
    """True when a bootstrap guess can't be verified, so nothing may be assumed.

    If the fetched snapshot matches HEAD, the guess is confirmed and we adopt
    it. If it differs, HEAD and GitHub disagree and there's no recorded base to
    say which one moved — so either answer loses data:

      - assume GitHub is ahead → the rebase finds nothing to replay, the branch
        lands on remote content, and unpushed local work is reverted and then
        pushed (precisely the failure this ref exists to prevent);
      - assume local is ahead → `push` sends a stale HEAD over a newer remote.

    So refuse, leaving the ref unset, and let the user's next command supply the
    answer. `-m overwrite` and `push` are each an explicit answer, so they pass.
    """
    if not snap.changed or mode == 'overwrite':
        return False
    err(f"Error: no recorded GitHub state ({REMOTE_REF} is unset), and HEAD "
        f"disagrees with {snap.item_label}: {snap.summary()}")
    err("Refusing to guess which side is ahead — either answer would lose data.")
    err(f"  Inspect:      git diff HEAD {snap.sha[:8]}")
    err("  Remote wins:  ghpr pull -m overwrite")
    err("  Local wins:   ghpr push")
    err(f"  Or set the base by hand: git update-ref {REMOTE_REF} <sha>")
    return True


def fetch(dry_run: bool, no_comments: bool) -> Snapshot:
    """Advance the remote-tracking ref to GitHub's current state."""
    owner, repo, number, item_type = resolve_item()
    base, bootstrap = resolve_base()
    err(f"Fetching {owner}/{repo}#{number}...")
    snap = build_snapshot(owner, repo, number, item_type, base, no_comments, keep=not dry_run)
    if bootstrap and bootstrap_is_ambiguous(snap):
        if dry_run:
            return snap
        exit(1)
    if not snap.changed:
        err(f"Already up to date with {snap.item_label}")
        if bootstrap:
            set_remote_ref(base)
        return snap
    if dry_run:
        err(f"[DRY-RUN] Would fetch from {snap.item_label}: {snap.summary()}")
    else:
        set_remote_ref(snap.sha)
        err(f"Fetched from {snap.item_label}: {snap.summary()}")
    return snap


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @flag('--no-comments', help='Skip fetching comments and review threads')
    @flag('-n', '--dry-run', help='Show what would be fetched, without moving the ref')
    def fetch_cmd(no_comments, dry_run):
        """Snapshot GitHub state into `refs/remotes/github/remote`, without touching the working tree."""
        fetch(dry_run, no_comments)
