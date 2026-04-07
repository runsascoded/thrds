# Spec: Per-request sleep / throttle with optional jitter

## Problem

Discord rate-limits edits on messages older than 1 hour (~3 per burst). Adding 10s sleep between requests avoids this entirely. Currently callers have to manage throttling themselves.

## Implementation

`SyncOptions.pace` already existed (delay between mutating API calls). Added `jitter: float = 0.0` — random uniform `[0, jitter)` added to each delay.

All three clients (`SlackClient`, `DiscordClient`, `BskyClient`) expose `pace` and `jitter` params on their `sync()` methods.
