# Spec: Handle Discord edit rate limits

## Problem

Discord rate-limits edits on messages older than 1 hour (error code 30046: "Maximum number of edits to messages older than 1 hour reached"). We hit this after 3 edits trying to re-chunk a thread.

## Options

1. **Retry with backoff**: catch code 30046, wait, retry. Discord returns `Retry-After` headers on 429s but this is a different limit (hard cap, not a rate bucket).

2. **Delete and re-post**: for thread replies (not OPs), delete old messages and post fresh. This is safe in Discord (no tombstones, unlike Slack). The `sync()` algorithm could detect this rate limit and fall back to delete+repost for remaining messages.

3. **Rate-limit aware sync**: track edit timestamps and throttle to stay under the limit. Downside: slow for large thread rewrites.

## Recommendation

Option 2 is simplest and most robust. When `sync()` encounters a 30046 during edit phase, it should:
1. Delete remaining existing messages (from end to current position)
2. Post new messages for the rest

This only applies to Discord — Slack doesn't have this rate limit on edits.

## Implementation

The `DiscordClient.edit` method should catch the 30046 error and raise a specific exception (e.g. `EditRateLimited`) that `sync()` can handle by switching to delete+repost mode.

Alternatively, `SyncOptions` could have a `force_repost: bool = False` flag that skips edits entirely and does delete+repost for all messages.
