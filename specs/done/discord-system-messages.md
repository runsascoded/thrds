# Spec: Filter Discord system messages in `list_messages`

## Problem

Discord threads have a "system message" at index 0 — an empty message that mirrors the parent channel message. `DiscordClient.list_messages` returns it, but it can't be edited (error code 50021: "Cannot execute action on a system message").

This causes `sync()` to try editing it as if it were a regular message, crashing on the first edit.

## Fix

`DiscordClient.list_messages` should filter out system messages. Discord message objects have a `type` field:
- `type: 0` = regular message (DEFAULT)
- `type: 21` = thread starter message (THREAD_STARTER_MESSAGE)
- Various other types for joins, pins, etc.

Filter to only return `type == 0` messages:

```python
def list_messages(self, thread_id: str) -> list[Message]:
    resp = self._curl("GET", f"/channels/{thread_id}/messages?limit=100")
    if not resp:
        return []
    return [
        Message(id=str(m["id"]), content=m.get("content", ""))
        for m in reversed(resp)
        if m.get("type", 0) == 0  # only regular messages
    ]
```

## Also consider

- The thread parent message (in the channel, not the thread) is managed separately from the thread replies. `sync()` should handle thread replies only; the parent/OP is a separate channel message. Maybe `DiscordClient.sync()` needs a `parent_message_id` parameter to edit the OP separately from the thread body.
