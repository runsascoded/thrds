"""`ghpr review` — local convenience ops on review threads.

These commands only edit local files; the actual GitHub sync happens on the
next `ghpr push`. Run them from inside a `gh/<num>/` clone.
"""

import click
from utz import err
from utz.cli import arg, opt

from .. import reviews


def _resolve_or_die(thread):
    head_id = reviews.find_thread(thread)
    if head_id is None:
        err(f"Error: no review thread matching '{thread}'")
        raise SystemExit(1)
    return head_id


def register(cli):
    """Register command with CLI."""

    @cli.group()
    def review():
        """Work with PR review threads locally (push syncs to GitHub)."""
        pass

    @review.command()
    @opt('-m', '--message', help='Reply body (skips opening $EDITOR)')
    @arg('thread')
    def reply(message, thread):
        """Draft a reply to THREAD (head-comment id or node id)."""
        head_id = _resolve_or_die(thread)
        path = reviews.new_reply_draft_path(head_id)
        if message is None:
            content = click.edit(text='') or ''
            if not content.strip():
                err("Aborted: empty reply")
                raise SystemExit(1)
        else:
            content = message if message.endswith('\n') else message + '\n'
        path.write_text(content)
        err(f"Wrote {path}. Review with `ghpr diff`, then `ghpr push` to post.")

    @review.command()
    @arg('thread')
    def resolve(thread):
        """Mark THREAD resolved (applied on next push)."""
        head_id = _resolve_or_die(thread)
        if reviews.set_thread_resolved(head_id, True):
            err(f"Marked thread {head_id} resolved. `ghpr push` to apply.")
        else:
            err(f"Thread {head_id} already resolved")

    @review.command()
    @arg('thread')
    def unresolve(thread):
        """Mark THREAD unresolved (applied on next push)."""
        head_id = _resolve_or_die(thread)
        if reviews.set_thread_resolved(head_id, False):
            err(f"Marked thread {head_id} unresolved. `ghpr push` to apply.")
        else:
            err(f"Thread {head_id} already unresolved")
