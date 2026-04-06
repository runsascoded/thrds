# Spec: `thrds` — Declarative thread sync for Slack and Discord

## Goal

A Python library for declaratively syncing a list of messages to a Slack or Discord thread. Given a desired thread state (list of message contents), it diffs against existing messages and applies the minimal edits/posts/deletes to converge.

## Core Data Model

```python
@dataclass
class Thread:
    """Desired state of a thread."""
    messages: list[str]  # Ordered list of message contents (first is OP/parent)
```

## Sync Algorithm

Given desired messages `M` and existing thread messages `N`:

1. For `i` in `range(min(M, N))`: edit existing message `i` to desired content `i` (skip if unchanged)
2. If `M > N`: post `M - N` new replies at the end
3. If `M < N`: delete extras from the end first, working backwards (reply N-1, N-2, ... down to M). Delete OP last (only if M == 0, i.e. full thread deletion).

This ensures:
- Message ordering is preserved
- Thread parent is never orphaned (replies deleted before OP)
- Minimal API calls (skip unchanged messages)
- No tombstone creation (edits, not delete+recreate)

## API

```python
from thrds import SlackClient, DiscordClient, Thread

# Slack
slack = SlackClient(token="xoxb-...", channel="C0AQC2VKEJF")
thread = Thread(messages=["OP text", "Reply 1", "Reply 2"])
result = slack.sync(thread, thread_ts="1775516040.743629")  # existing thread
# result.thread_ts, result.message_ts_list

# Or create new thread
result = slack.sync(thread)

# Discord
discord = DiscordClient(token="Bot ...", channel_id="1489279547689140505")
thread = Thread(messages=["OP text", "Reply 1", "Reply 2"])
result = discord.sync(thread, thread_id="1490821926288097503")

# Dry-run (print what would happen, no API calls)
result = slack.sync(thread, thread_ts="...", dry_run=True)
```

## Return Value

```python
@dataclass
class SyncResult:
    """Result of syncing a thread."""
    thread_id: str          # thread_ts (Slack) or thread_id (Discord)
    message_ids: list[str]  # ts values (Slack) or message IDs (Discord)
    actions: list[Action]   # what was done: Edit, Post, Delete, Skip
```

## Client Interface

```python
class ThreadClient(Protocol):
    def list_messages(self, thread_id: str) -> list[Message]
    def post(self, content: str, thread_id: str | None = None) -> Message
    def edit(self, message_id: str, content: str) -> Message
    def delete(self, message_id: str) -> None
```

### Slack-specific

- Uses `chat.postMessage`, `chat.update`, `chat.delete`
- Thread parent is `thread_ts`; replies use `thread_ts` parameter
- `conversations.replies` to list existing messages
- Message IDs are `ts` strings

### Discord-specific

- Uses curl subprocess (urllib blocked by Cloudflare)
- Thread parent creates a Discord thread via `POST .../messages/{id}/threads`
- Thread ID is the channel ID of the thread
- `GET /channels/{thread_id}/messages` to list existing
- Message IDs are snowflake strings
- 2000 char message limit (Slack is 4000)
- `flags: 4` for SUPPRESS_EMBEDS

## Options

```python
@dataclass
class SyncOptions:
    suppress_embeds: bool = False   # Discord: suppress link previews
    dry_run: bool = False           # Print actions without executing
    thread_name: str | None = None  # Discord: thread name (for creation)
```

## Metadata Persistence

The library itself doesn't persist state — it returns `SyncResult` with all IDs. Callers (like `summarize.py`) are responsible for saving to `meta.json` or similar.

## Package Structure

```
thrds/
  __init__.py       # Thread, SyncResult, SyncOptions exports
  core.py           # Thread, sync algorithm, Action types
  slack.py          # SlackClient
  discord.py        # DiscordClient
  protocol.py       # ThreadClient protocol
```

## Dependencies

- No required dependencies beyond stdlib
- Slack: uses `urllib.request` (stdlib)
- Discord: uses `subprocess` + `curl` (to avoid Cloudflare blocks)

## CLI (optional, future)

```bash
thrds sync slack --channel C0AQC2VKEJF --thread-ts 1775516040.743629 msg1.txt msg2.txt msg3.txt
thrds sync discord --channel 1489279547689140505 --thread-id 1490821926288097503 msg1.txt msg2.txt
```

## Migration Path

- `hccs/crashes` `ChannelClient.sync_crash()` → use `SlackClient.sync()`
- `discord-agent/summarize.py` `post_to_discord()` / `post_to_slack()` → use `thrds`
