from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import ThreadClient


class EditRateLimited(Exception):
    """Raised when an edit is rate-limited (e.g. Discord code 30046)."""


class OrphanedRepliesError(Exception):
    """Raised when attempting to delete a message that has thread replies."""

    def __init__(self, message_id: str, reply_count: int):
        self.message_id = message_id
        self.reply_count = reply_count
        super().__init__(
            f"Refusing to delete message {message_id}: "
            f"it has {reply_count} thread replies that would be orphaned. "
            f"Pass orphans_ok=True to delete anyway."
        )


class ActionType(Enum):
    SKIP = "skip"
    EDIT = "edit"
    POST = "post"
    DELETE = "delete"


@dataclass
class Action:
    type: ActionType
    index: int
    message_id: str | None = None
    content: str | None = None
    prior_content: str | None = None

    def format(self, color: bool = True) -> str:
        """Render a human-readable preview line for this action.

        EDIT and DELETE show the prior content (``-``); POST and EDIT
        show the new content (``+``); SKIP just notes the index.
        Multi-line content gets the prefix on every line.
        """
        RED, GREEN, RESET = ("\033[31m", "\033[32m", "\033[0m") if color else ("", "", "")
        header = f"{self.type.value.upper()} [{self.index}]"

        def prefix_lines(s: str, char: str, col: str) -> str:
            return "\n".join(f"  {col}{char}{line}{RESET}" for line in s.split("\n"))

        if self.type is ActionType.POST:
            return f"{header}\n{prefix_lines(self.content or '', '+', GREEN)}"
        if self.type is ActionType.EDIT:
            prior = prefix_lines(self.prior_content or "", "-", RED)
            new = prefix_lines(self.content or "", "+", GREEN)
            return f"{header}\n{prior}\n{new}"
        if self.type is ActionType.DELETE:
            return f"{header}\n{prefix_lines(self.prior_content or '', '-', RED)}"
        if self.type is ActionType.SKIP:
            return f"{header} (unchanged)"
        raise ValueError(f"Unknown action type: {self.type}")


@dataclass
class Thread:
    """Desired state of a thread."""
    messages: list[str]


@dataclass
class Message:
    """An existing message in a thread.

    ``editable=False`` marks messages the sync client cannot edit or delete
    (typically because they were authored by another user/bot). Such
    messages are preserved in place — never included in the `sync()`
    reconcile, never counted against the desired message slots.
    """
    id: str
    content: str
    editable: bool = True


@dataclass
class SyncResult:
    """Result of syncing a thread."""
    thread_id: str
    message_ids: list[str]
    actions: list[Action] = field(default_factory=list)

    def format_preview(self, color: bool = True, prefix: str = "") -> str:
        """Render a colored multi-line preview of all actions.

        ``prefix`` is prepended to each line (e.g. a per-thread identifier).
        """
        lines: list[str] = []
        for action in self.actions:
            for line in action.format(color=color).split("\n"):
                lines.append(prefix + line)
        return "\n".join(lines)


@dataclass
class SyncOptions:
    suppress_embeds: bool = False
    suppress_unfurls: bool = True
    dry_run: bool = False
    thread_name: str | None = None
    pace: float = 0.0
    jitter: float = 0.0


def sync(
    client: ThreadClient,
    desired: Thread,
    thread_id: str | None = None,
    options: SyncOptions | None = None,
) -> SyncResult:
    """Sync a thread to the desired state using minimal API calls."""
    opts = options or SyncOptions()
    actions: list[Action] = []
    message_ids: list[str] = []
    mutated = False

    def _pace():
        nonlocal mutated
        if mutated and opts.pace > 0:
            delay = opts.pace + random.uniform(0, opts.jitter)
            time.sleep(delay)
        mutated = True

    # Get existing messages (if thread exists). Foreign (non-editable)
    # messages — e.g. a human interjecting in a bot-managed thread — are
    # filtered out of the reconcile. They stay in place: never edited,
    # never deleted, and not counted against the desired message slots.
    if thread_id is not None:
        all_existing = client.list_messages(thread_id)
    else:
        all_existing = []
    existing = [m for m in all_existing if m.editable]

    M = len(desired.messages)
    N = len(existing)

    # Phase 1: Delete extras from the end (backwards, OP last)
    if M < N:
        for i in range(N - 1, M - 1, -1):
            msg = existing[i]
            action = Action(
                type=ActionType.DELETE,
                index=i,
                message_id=msg.id,
                prior_content=msg.content,
            )
            actions.append(action)
            if not opts.dry_run:
                _pace()
                client.delete(msg.id)

    # Phase 2: Edit overlapping messages
    overlap = min(M, N)
    repost_from: int | None = None
    for i in range(overlap):
        if existing[i].content == desired.messages[i]:
            actions.append(Action(
                type=ActionType.SKIP,
                index=i,
                message_id=existing[i].id,
                content=desired.messages[i],
            ))
            message_ids.append(existing[i].id)
        else:
            action = Action(
                type=ActionType.EDIT,
                index=i,
                message_id=existing[i].id,
                content=desired.messages[i],
                prior_content=existing[i].content,
            )
            actions.append(action)
            if opts.dry_run:
                message_ids.append(existing[i].id)
            else:
                try:
                    _pace()
                    result_msg = client.edit(existing[i].id, desired.messages[i])
                except EditRateLimited:
                    # Fall back to delete+repost for this and all remaining messages
                    repost_from = i
                    break
                message_ids.append(result_msg.id)

    # Phase 2b: Delete+repost fallback (on edit rate limit)
    if repost_from is not None:
        # Delete remaining existing messages from end to repost_from
        for j in range(overlap - 1, repost_from - 1, -1):
            msg = existing[j]
            actions.append(Action(
                type=ActionType.DELETE,
                index=j,
                message_id=msg.id,
                prior_content=msg.content,
            ))
            _pace()
            client.delete(msg.id)
        # Post replacements (and any new messages beyond overlap)
        for j in range(repost_from, M):
            action = Action(type=ActionType.POST, index=j, content=desired.messages[j])
            actions.append(action)
            _pace()
            result_msg = client.post(desired.messages[j], thread_id=thread_id)
            message_ids.append(result_msg.id)
        return SyncResult(
            thread_id=thread_id or "",
            message_ids=message_ids,
            actions=actions,
        )

    # Phase 3: Post new messages at the end
    if M > N:
        # If no thread exists yet, first message creates it
        start = N
        if thread_id is None and N == 0 and M > 0:
            action = Action(
                type=ActionType.POST,
                index=0,
                content=desired.messages[0],
            )
            actions.append(action)
            if opts.dry_run:
                message_ids.append("<new>")
                thread_id = "<new>"
            else:
                _pace()
                result_msg = client.post(desired.messages[0])
                thread_id = result_msg.id
                message_ids.append(result_msg.id)
            start = 1

        for i in range(start, M):
            action = Action(
                type=ActionType.POST,
                index=i,
                content=desired.messages[i],
            )
            actions.append(action)
            if opts.dry_run:
                message_ids.append("<new>")
            else:
                _pace()
                result_msg = client.post(desired.messages[i], thread_id=thread_id)
                message_ids.append(result_msg.id)

    return SyncResult(
        thread_id=thread_id or "",
        message_ids=message_ids,
        actions=actions,
    )
