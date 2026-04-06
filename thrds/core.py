from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import ThreadClient


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


@dataclass
class Thread:
    """Desired state of a thread."""
    messages: list[str]


@dataclass
class Message:
    """An existing message in a thread."""
    id: str
    content: str


@dataclass
class SyncResult:
    """Result of syncing a thread."""
    thread_id: str
    message_ids: list[str]
    actions: list[Action] = field(default_factory=list)


@dataclass
class SyncOptions:
    suppress_embeds: bool = False
    dry_run: bool = False
    thread_name: str | None = None


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

    # Get existing messages (if thread exists)
    if thread_id is not None:
        existing = client.list_messages(thread_id)
    else:
        existing = []

    M = len(desired.messages)
    N = len(existing)

    # Phase 1: Delete extras from the end (backwards, OP last)
    if M < N:
        for i in range(N - 1, M - 1, -1):
            msg = existing[i]
            action = Action(type=ActionType.DELETE, index=i, message_id=msg.id)
            actions.append(action)
            if not opts.dry_run:
                client.delete(msg.id)

    # Phase 2: Edit overlapping messages
    overlap = min(M, N)
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
            )
            actions.append(action)
            if opts.dry_run:
                message_ids.append(existing[i].id)
            else:
                result_msg = client.edit(existing[i].id, desired.messages[i])
                message_ids.append(result_msg.id)

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
                result_msg = client.post(desired.messages[i], thread_id=thread_id)
                message_ids.append(result_msg.id)

    return SyncResult(
        thread_id=thread_id or "",
        message_ids=message_ids,
        actions=actions,
    )
