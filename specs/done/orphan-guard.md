# Guard against orphaning thread replies on message deletion

## Problem

When `SlackClient.delete()` is called on a message that has thread replies,
those replies become orphaned — they remain visible in the channel but their
parent is gone, making them confusing/unreachable. This happened in practice
when a bot deleted a top-level message without realizing it had replies.

## Proposed change

Add an `orphans_ok: bool = False` parameter to `SlackClient.delete()`. When
`False` (default), before deleting a message, call `conversations.replies` to
check if it has any replies. If it does, raise:

```python
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
```

When `orphans_ok=True`, skip the check and delete unconditionally (current
behavior).

### Cost

One extra `conversations.replies` RPC per delete when `orphans_ok=False`. This
only matters for direct `client.delete()` calls — the `sync()` function
operates on thread replies (not OPs), so it should pass `orphans_ok=True`
internally since it knows the messages it's deleting are leaves, not parents.

### Files to change

- **`thrds/slack.py`**: `SlackClient.delete()` — add `orphans_ok` param + guard
- **`thrds/core.py`**: `sync()` — pass `orphans_ok=True` on its internal
  `client.delete()` calls (lines 152, 199) since those are always thread replies
- **`thrds/protocol.py`**: update `ThreadClient.delete` protocol signature if it
  exists there

### Edge case

`conversations.replies` on a non-threaded message (no replies) returns just the
message itself (1 result). So the check is: `len(replies) > 1` means it has
child replies.
