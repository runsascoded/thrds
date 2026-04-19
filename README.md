# thrds

Declarative thread sync for Slack, Discord, and Bluesky.

Given a desired thread state (list of message contents), diffs against existing messages and applies minimal edits/posts/deletes to converge.

## Install

```bash
pip install thrds            # Core only (zero deps)
pip install thrds[bsky]      # + Bluesky (atproto)
```

Slack and Discord clients use only stdlib (`urllib`) and `curl` subprocess respectively — no extra deps needed.

## Usage

```python
from thrds import SlackClient, Thread

slack = SlackClient(token="xoxb-...", channel="C0AQC2VKEJF")
thread = Thread(messages=["OP text", "Reply 1", "Reply 2"])

# Create new thread
result = slack.sync(thread)

# Update existing thread (edits changed messages, appends new, deletes extras)
result = slack.sync(thread, thread_ts="1775516040.743629")
```

### Discord

```python
from thrds import DiscordClient, Thread

discord = DiscordClient(token="your-bot-token", channel_id="1489279547689140505")
thread = Thread(messages=["OP text", "Reply 1", "Reply 2"])
result = discord.sync(thread, thread_id="1490821926288097503")
```

### Bluesky

```python
from thrds import BskyClient, Thread

bsky = BskyClient(handle="you.bsky.social", password="app-password")
thread = Thread(messages=["Root post", "Reply 1"])
result = bsky.sync(thread)
```

Bluesky doesn't support editing posts — the sync algorithm automatically falls back to delete+repost when content changes.

### Linked summary threads

Post summary bullets that link to detail messages in the same thread:

```python
from thrds import LinkedThread, Section

linked = LinkedThread(
    summary_prefix="# Daily Digest",
    sections=[
        Section(title="Topic A", summary="Brief summary", body="Full detail text..."),
        Section(title="Topic B", summary="Another summary", body="More details..."),
    ],
)

# Discord: summary bullets use [**Title**](url) markdown links
result = discord.sync_linked(linked, thread_id="...", guild_id="...")

# Slack: summary bullets use <url|*Title*> mrkdwn links
result = slack.sync_linked(linked, thread_ts="...")
```

Two-phase sync: posts all messages with placeholder links, then edits summaries with real links once message IDs are known.

### Dry run / diff preview

```python
result = slack.sync(thread, thread_ts="...", dry_run=True)
print(result.format_preview(color=True, prefix="thread: "))
```

```
thread: SKIP [0] (unchanged)
thread: EDIT [1]
thread:   -old message text
thread:   +new message text
thread: POST [2]
thread:   +entirely new message
```

Each `Action` carries `prior_content` (for EDIT/DELETE) alongside `content`, enabling colored unified-diff output via `action.format()`.

## Sync algorithm

Given desired messages `M` and existing thread messages `N`:

1. **Delete** extras from the end (backwards — replies before OP)
2. **Edit** overlapping messages where content changed (skip unchanged)
3. **Post** new messages at the end

Foreign (non-editable) messages — e.g. human replies in a bot thread — are automatically skipped. The sync only operates on the bot's own messages, leaving everyone else's untouched.

## Features

- **Foreign message preservation**: Non-bot messages in threads are skipped during sync (no more `cant_update_message` errors)
- **Rate limit handling**: Slack 429 retry with `Retry-After`, configurable `pace` and `jitter` between API calls
- **Edit rate limit fallback**: Discord's 30046 error (edit limit on old messages) triggers automatic delete+repost
- **Linked summary threads**: `sync_linked()` for summary-with-links threads on Discord and Slack
- **Diff preview**: `Action.format()` and `SyncResult.format_preview()` for colored diff output
- **Orphan guard**: Slack `delete()` checks for thread replies before deleting (raises `OrphanedRepliesError`)
- **Unfurl/embed suppression**: Slack link previews and Discord embeds suppressed via options
- **Discord system message filtering**: Thread starter messages filtered from `list_messages`
- **Bot token prefix**: Discord `Bot ` prefix auto-prepended
- **Metadata support**: Slack message metadata passthrough

## Used by

- [hudcostreets/nj-crashes] — Slack crash-notification threads (`SlackClient.sync()`)
- [Open-Athena/marin-discord] — Discord summary threads (`DiscordClient.sync_linked()`)

## API

### `SyncResult`

```python
@dataclass
class SyncResult:
    thread_id: str          # thread_ts (Slack), thread channel ID (Discord), AT URI (Bluesky)
    message_ids: list[str]  # Per-message IDs
    actions: list[Action]   # What was done: Edit, Post, Delete, Skip
```

### `Action`

```python
@dataclass
class Action:
    type: ActionType        # SKIP, EDIT, POST, DELETE
    index: int
    message_id: str | None
    content: str | None         # Desired text (POST, EDIT, SKIP)
    prior_content: str | None   # Previous text (EDIT, DELETE)
```

### `SyncOptions`

| Option | Default | Description |
|--------|---------|-------------|
| `dry_run` | `False` | Print actions without executing |
| `pace` | `0.0` | Seconds between mutating API calls |
| `jitter` | `0.0` | Random additional delay (0 to `jitter`) added to `pace` |
| `suppress_embeds` | `False` | Discord: suppress link previews |
| `suppress_unfurls` | `True` | Slack: suppress link previews |

[hudcostreets/nj-crashes]: https://github.com/hudcostreets/nj-crashes
[Open-Athena/marin-discord]: https://github.com/Open-Athena/marin-discord
