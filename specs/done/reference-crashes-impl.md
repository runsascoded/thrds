# Reference: Working Slack thread sync in `hccs/crashes`

## Location

`/Users/ryan/c/hccs/crashes/njsp/cli/slack/channel_client.py`

## Key patterns to adopt

- `sync_crash()` method (line ~354-400): position-based edit vs create logic
- Uses `WebClient` from `slack_sdk` (handles GET/POST correctly)
- `self.update_msg(ts=ts, ...)` for edits
- `self.post_msg(...)` for new messages, captures `thread_ts` from first post
- `self.delete_msg(ts=ts)` for cleanup
- `overwrite_existing` parameter controls force-rewrite behavior

## Slack API notes from that impl

- `conversations.replies` is called via `slack_sdk.WebClient` which handles GET properly
- Thread parent `ts` becomes `thread_ts` for all replies
- `metadata` field used to tag messages with crash IDs for lookup
