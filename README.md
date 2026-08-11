# thrds (Python)

Declarative thread sync for Slack, Discord, and Bluesky.

Given a desired thread state (list of message contents), diffs against existing messages and applies minimal edits/posts/deletes to converge.

> A TypeScript port (Slack subset) lives on the [`ts` branch][ts-branch], published on npm as [`@rdub/thrds`][npm]. Both impls share `tests/fixtures/sync.json` as the cross-language contract for the diff/edit/post/delete algorithm.

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

## CLI

`thrds` also ships a CLI for drafting multi-thread Slack posts locally + syncing them to a staging private channel + promoting to a real prod channel. One session per `.md` file lives in `<git-root-or-cwd>/thrds/<slug>/` with its own private git repo and (default) a secret gist mirror for version history.

```bash
thrds init draft.md              # scaffold session dir + gist mirror
thrds push                       # sync to staging PC (terraform)
thrds pull --write               # pull edits back → .md
thrds push --prod --channel #foo # sync to prod (additive)
thrds diff --prod --channel #foo # see what would change
thrds archive                    # archive the staging PC
thrds list-sessions #foo         # what thrds sessions exist in #foo
thrds recover -i <sid> #foo      # rebuild a lost session from Slack metadata
```

`thrds slack …` is a low-level CRUD subgroup for ad-hoc Slack operations (finding a message's ts, deleting a test post, posting one-off mrkdwn) — a first-class alternative to hand-rolled `chat.*` heredocs. All verbs default to **raw mrkdwn** (send verbatim); pass `-m` to opt into local-md → Slack-mrkdwn conversion (the opposite of the session verbs' default — see [`raw-mrkdwn-passthrough`][raw-spec]).

```bash
thrds slack history #foo -n 10           # last 10 messages (ts, sender, text)
thrds slack thread  #foo 1783.1          # OP + replies as a table (`-j` = JSON)
thrds slack rm      #foo 1783.1 1783.2   # delete msg(s); `-f` = orphans_ok
thrds slack post    #foo '*bold*'        # raw mrkdwn (`-m` = convert md first)
thrds slack post    #foo 'hi' -u 'Bot' -i https://cdn.example/a.png -t 1783.0
thrds slack edit    #foo 1783.1 'new'    # edit; raw by default
thrds slack permalink #foo 1783.1        # get workspace permalink URL
```

[raw-spec]: specs/done/raw-mrkdwn-passthrough.md

### Slack app scopes

The CLI needs a **user token** (`xoxp-`, exposed as `SLACK_THRDS_USER_TOKEN`) — `chat.update` only edits messages authored by the token's owner, so a bot token can't edit human-typed drafts, which is the whole point.

Add these under **OAuth & Permissions → User Token Scopes**:

| Scope | Needed for |
| --- | --- |
| `chat:write` | Post + edit messages (all sync verbs) |
| `groups:write` | Create + archive staging private channels (`init`, `push`, `archive`) |
| `groups:read` | Read private channel history + resolve `#name` → `C…` for private channels |
| `channels:read` | Resolve `#name` → `C…` for public channels (only if pushing/pulling public) |
| `users:read` | Resolve foreign-author names on `pull` |
| `emoji:read` | Download custom workspace emoji on `pull` |
| `chat:write.customize` | Per-message `username` / `icon_url` / `icon_emoji` overrides (via `Msg`) |
| `reactions:read` | `SenderChangePolicy` pre-flight (`sync` aborts if a target of a sender-change repost has reactions) |

Metadata visibility is app-scoped (Slack only returns your app's metadata to your app), so `recover` needs **no** additional scope beyond the ones above.

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
[ts-branch]: https://github.com/runsascoded/thrds/tree/ts
[npm]: https://www.npmjs.com/package/@rdub/thrds
