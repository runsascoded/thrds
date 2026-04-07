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

### Dry run

```python
result = slack.sync(thread, thread_ts="...", dry_run=True)
for action in result.actions:
    print(action.type, action.index, action.content)
```

## Sync algorithm

Given desired messages `M` and existing thread messages `N`:

1. **Delete** extras from the end (backwards — replies before OP)
2. **Edit** overlapping messages where content changed (skip unchanged)
3. **Post** new messages at the end

This ensures minimal API calls, preserved ordering, and no orphaned thread parents.

## Features

- **Rate limit handling**: Slack 429 retry with `Retry-After`, configurable pacing between API calls
- **Edit rate limit fallback**: Discord's 30046 error (edit limit on old messages) triggers automatic delete+repost
- **Unfurl suppression**: Slack link previews suppressed by default
- **Discord system message filtering**: Thread starter messages filtered from `list_messages`
- **Bot token prefix**: Discord `Bot ` prefix auto-prepended
- **Metadata support**: Slack message metadata passthrough

## Used by

- [hudcostreets/nj-crashes] — Slack crash-notification threads (`SlackClient.sync()`)
- [Open-Athena/marin-discord] — Discord summary threads (`DiscordClient.sync()`)

## API

### `SyncResult`

```python
@dataclass
class SyncResult:
    thread_id: str          # thread_ts (Slack), thread channel ID (Discord), AT URI (Bluesky)
    message_ids: list[str]  # Per-message IDs
    actions: list[Action]   # What was done: Edit, Post, Delete, Skip
```

### `SyncOptions`

| Option | Default | Description |
|--------|---------|-------------|
| `dry_run` | `False` | Print actions without executing |
| `pace` | `0.0` | Seconds between mutating API calls |
| `suppress_embeds` | `False` | Discord: suppress link previews |
| `suppress_unfurls` | `True` | Slack: suppress link previews |

[hudcostreets/nj-crashes]: https://github.com/hudcostreets/nj-crashes
[Open-Athena/marin-discord]: https://github.com/Open-Athena/marin-discord
