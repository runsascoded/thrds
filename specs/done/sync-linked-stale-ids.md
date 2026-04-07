# Spec: `sync_linked` edit-back hits "Unknown Message" (code 10008)

## Repro

Mar 24 thread in marin-discord. Thread has 14 live messages but accumulated stale state from previous rate-limit fallbacks (some messages were deleted+reposted with new IDs).

```python
linked = LinkedThread(summary_prefix='', sections=sections)
dr = discord.sync_linked(linked, thread_id=thread_id, suppress_embeds=True, pace=2)
# → RuntimeError: Discord API error: Unknown Message (code 10008)
```

Error occurs during the edit-back phase (step 2), where `sync_linked` edits summary messages with real detail links.

## Root cause

`sync_linked` internally calls `sync()` (phase 1), which may delete+repost messages during its `EditRateLimited` fallback. The phase 1 `SyncResult.message_ids` should contain the new IDs, but the edit-back code in phase 2 may be using stale IDs from somewhere else.

## Expected behavior

After phase 1 `sync()` completes, all IDs in `SyncResult.message_ids` should be valid, live message IDs. Phase 2 should only use those IDs for the edit-back. If phase 1 did any delete+repost, the new IDs should be in the result.

## Additional issue

`sync_linked` also currently puts `summary_prefix` content as the first thread message, but for Discord threads the prefix is the OP/parent message (managed separately, not part of the thread replies). When `summary_prefix` is non-empty, it gets duplicated: once as the OP and once as the first thread reply.

Fix: when `summary_prefix` is empty string, `sync_linked` should only sync S bullets + M details as thread replies. The caller manages the OP separately.
