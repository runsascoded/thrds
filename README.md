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

`thrds` ships a CLI split into platform subgroups. Today: `thrds slack …` (the primary workflow) and `thrds capture …` (gist-only trajectory, no platform target — for drafting posts you'll paste manually while still capturing iteration history). One session per `.md` file lives in `<git-root-or-cwd>/thrds/<slug>/` with its own private git repo and (default) a secret gist mirror.

**`thrds slack …`** — draft multi-thread Slack posts locally, sync to a staging private channel, promote to a real prod channel:

Installing `thrds` also installs **`slck`**, which *is* the `thrds slack` group — same click group object, so every verb, flag and help string is shared and `slck push` ≡ `thrds slack push`. (An alias list would need one entry per verb and go stale as verbs are added; an entry point picks up new ones for free.)

```bash
slck init draft.md                     # scaffold session dir + gist mirror
slck migrate                           # split the doc into per-thread NN-slug.md files
slck push                              # sync every thread to staging PC (terraform)
slck pull                              # pull edits back → each thread's file (-n to preview)
slck status                            # per-thread state + resolved destination
slck promote cw-mpu                    # post ONE thread to its target (confirms first)
slck drop cw-summary                   # mark a thread abandoned without posting
slck reopen cw-mpu                     # un-finalize a posted/dropped thread
slck reorder                           # renumber thread files to a gapless 01..NN
slck archive                           # archive staging (once all threads are terminal)
slck list-sessions #foo                # what thrds sessions exist in #foo
slck recover -i <sid> #foo             # rebuild a lost session from Slack metadata
```

**Converting an existing session.** `migrate` converts the working tree; `replay` converts the *git history*, so a session kept as a writing example can be read without learning the retired `===` syntax:

```bash
thrds slack replay -n          # plan + verify, write nothing
thrds slack replay             # write the rewritten history to branch `per-thread`
git log --stat per-thread      # inspect, then move the remote yourself
```

Every commit is rebuilt with the doc split into thread files, preserving message, author, committer and dates. Indices are assigned **globally** from the final commit's ordering, so a slug keeps one number for all time and a commit where a thread doesn't exist yet simply has a gap — numbering each commit independently would renumber every thread below an insertion, and a rename is precisely what breaks the per-file history the layout exists to produce. Before writing anything it verifies that every commit's new files parse back to exactly the threads the old doc parsed to; it refuses if not, and never force-pushes.

**One file per thread.** A session directory holds `01-slug.md`, `02-slug.md`, … — one Slack thread each (`+++` still separates replies *within* a thread). Per-file git history then reads as "*this message* went v2→v3" rather than "the doc changed", which is the point when the gist history is the artifact you're keeping.

**Destination is a property of the thread, not the session.** Each thread records its own `{channel, thread_ts?}` target in `thrds.json`; the session-level `prod_channel` is just a default for threads that don't set one. A target with no `thread_ts` posts a new top-level message; with one, the thread's messages go in as replies to that existing message — so "draft a considered reply to someone else's post" and "batch six messages into one channel" are the same mechanism.

**Staging-only chrome.** Staged messages carry one extra line saying where the draft is aimed, what it became once posted, and which file in the gist it is:

```
→ #oa-amazon-trainium · posted · 01-mfu.md
→ (#marin-alerts) · posted · 02-cw-summary.md
```

In the second, the arrow itself links to the message being replied to — the `thread_ts` is what a machine needs and a human never reads. The gist link deep-links the file (`#file-01-mfu-md`), not just the gist. Configured in `thrds.json`, never written into doc content:

```jsonc
"staging_chrome": {
  "gist_link": true, "target_link": true, "posted_link": true,
  "finalize_terminal": true, "style": "footer"
}
```

**The chrome line is an input, not just a readout.** Edit it in Slack and the next `pull` acts on it:

| write | effect |
| --- | --- |
| `→ #some-channel` | retarget the thread |
| `→ <paste a channel link>` | same |
| `→ <paste a message link>` | aim it *into* that thread |
| `→ #chan · 04-idea.md` | rename/renumber the thread's file |

A push renders it back to canonical form, so the hand-typed shape only has to survive one round trip. It's accepted as the **first or last** line, since leading with where a draft is going is the natural way to write one.

**Start a thread by writing one in the staging channel.** Post a top-level message there with a chrome line and the next `pull` adopts it: creates its `NN-slug.md` (named by the chrome line's filename, or slugified from the opening words), records its target, and takes over syncing it. A message with no chrome line stays a note to self — that's what separates the two.

**Reordering.** `slck reorder` renumbers files to a gapless `01..NN`; `slck reorder gamma alpha` puts those first. Only files move — `thrds.json` is keyed by slug, so staging pointers, targets and posted timestamps all follow their thread. Renaming is cheap because a commit records a *tree*: `git log --follow` reconstructs each thread's history across the rename.

**Finalizing.** Once a thread is `posted` or `dropped`, its staged copy re-renders with chrome in a `context` block. Slack strips the Edit affordance from any message carrying blocks — a liability for a draft, and exactly the point for one that's already gone out: the staged copy visibly locks. `slck reopen <slug>` moves it back to `draft` (keeping `posted_ts`, so a later `promote` syncs the existing message rather than posting a second one), and the next push unlocks it. Set `finalize_terminal: false` to keep everything editable.

Two things keep chrome out of prod, since it's in the message rather than beside it:

- **Stripped on pull.** The line is appended after md→mrkdwn conversion and removed before the reverse, so neither direction of the converter ever sees it. (For a finalized message the body is read from its section block — Slack flattens `text` to a one-line notification fallback whenever blocks are present.)
- **Fail closed at the boundary.** `promote` refuses to post a body that still carries a chrome line. Stripping is a step that can fail open; publishing a secret-gist URL to a real channel is worth an assertion.

**Prod push is per-thread and never whole-doc.** `promote` resolves the destination, prints it with the exact body, asks, then posts only that thread — and never archives the staging channel, because your other drafts are still live. Archiving is its own verb, refusing until every thread is `posted` or `dropped` (`-f` overrides).

`thrds slack …` also exposes low-level CRUD verbs for ad-hoc operations (finding a message's ts, deleting a test post, posting one-off mrkdwn) — a first-class alternative to hand-rolled `chat.*` heredocs. All CRUD verbs default to **raw mrkdwn** (send verbatim); pass `-m` to opt into local-md → Slack-mrkdwn conversion (the opposite of the session verbs' default — see [`raw-mrkdwn-passthrough`][raw-spec]).

```bash
thrds slack history #foo -n 10           # last 10 messages (ts, sender, text)
thrds slack thread  #foo 1783.1          # OP + replies as a table (`-j` = JSON)
thrds slack rm      #foo 1783.1 1783.2   # delete msg(s); `-f` = orphans_ok
thrds slack post    #foo '*bold*'        # raw mrkdwn (`-m` = convert md first)
thrds slack post    #foo 'hi' -u 'Bot' -i https://cdn.example/a.png -t 1783.0
thrds slack edit    #foo 1783.1 'new'    # edit; raw by default
thrds slack permalink #foo 1783.1        # get workspace permalink URL
```

**`thrds capture …`** — capture-only sessions: same on-disk shape (git repo + gist mirror), no platform posting. Useful when the destination is somewhere `thrds` doesn't (yet) integrate with, but you still want the doc's iteration history captured to a gist:

```bash
thrds capture init draft.md      # scaffold session dir + gist mirror (no channel)
# ... edit draft.md ...
thrds capture push               # commit doc changes and push to the gist
thrds capture open               # browse the gist
```

**`thrds discord …`** — capture + MD-compat lint for Discord. Discord asymmetry: prod delivery is copy-paste (self-bots are ToS-prohibited), so there's no `push`. `render` prints the doc to stdout (idiomatic: `thrds discord render | pbcopy`) and auto-runs `lint` alongside (tables and raw `@name` — two constructs Discord's user-message renderer drops on the floor; masked links `[text](url)` do render in normal user messages since Discord's 2023 markdown update):

```bash
thrds discord init draft.md      # scaffold session dir + gist (no channel/bot)
# ... edit draft.md, iterate ...
thrds discord lint               # just the MD-compat warnings
thrds discord render | pbcopy    # MD → clipboard (warnings → stderr)
thrds discord open               # browse the gist
```

**`thrds bsky …`** — same shape for Bluesky. Different lints: bsky's chief drafting pain is the **300-char post limit** (per paragraph), so `bsky lint` flags paragraphs that exceed it. Also warns on masked links (bsky auto-linkifies bare URLs via facets, so `[text](url)` renders as literal text):

```bash
thrds bsky init draft.md         # scaffold session dir + gist
thrds bsky lint                  # length + link warnings
thrds bsky render | pbcopy       # MD → clipboard
thrds bsky open                  # browse the gist
```

Every session's `platform` is stamped into `thrds.json` at init and guarded on every subsequent verb — running `thrds slack push` inside a capture-inited session errors immediately with a clear message rather than trying and failing halfway through.

[raw-spec]: specs/done/raw-mrkdwn-passthrough.md

### Slack tokens + scopes

The CLI reads the Slack token from `THRDS_SLACK_TOKEN` (a deprecated alias `SLACK_THRDS_USER_TOKEN` still works with a one-time warning). Which token type you need depends on which verbs you use:

- **User token** (`xoxp-…`) — needed for the **session verbs** (`slack init` / `push` / `pull` / `diff` / `archive` / `list-sessions` / `recover` / `open`). The session workflow's whole point is "draft locally in `.md`, sync to a staging PC, tweak the posts in Slack (as you), pull back, push again"; because those in-Slack tweaks are your Slack user's own posts, only a token you own can `chat.update` them.
- **Bot token** (`xoxb-…`) — sufficient for the **`slack` CRUD verbs** (`history` / `thread` / `rm` / `post` / `edit` / `permalink`) as long as the bot is only editing / deleting its own posts. Also sufficient for programmatic `SlackClient.sync()` / `sync_linked()` when the bot owns the content lifecycle end-to-end (bot renders, bot posts, bot reconciles).

Add scopes under **OAuth & Permissions** — under **User Token Scopes** for a user token, **Bot Token Scopes** for a bot token. All scopes have the same name in both places.

| Scope | Needed for | Session verbs | CRUD / `sync()` |
| --- | --- | :---: | :---: |
| `chat:write` | Post / edit / delete messages | ✓ | ✓ |
| `groups:write` | Create + archive staging PCs | ✓ (`slack init`, `push`, `archive`) | — |
| `groups:read` | Read + resolve `#name` for private channels | ✓ | ✓ |
| `channels:read` | Resolve `#name` for public channels | ✓ (if pushing/pulling public) | ✓ (public channels) |
| `users:read` | Resolve foreign-author names on `pull` | ✓ (`pull`) | — |
| `emoji:read` | Download custom workspace emoji on `pull` | ✓ (`pull`) | — |
| `chat:write.customize` | Per-message `username` / `icon_url` / `icon_emoji` | If used | If used (`slack post -u`/`-i`/`-e`) |
| `reactions:read` | `SenderChangePolicy` pre-flight (library) | — | If using aggressive-mode `sync` |

Metadata visibility is app-scoped (Slack only returns your app's metadata to your app), so `slack recover` needs **no** additional scope beyond the ones above.

### `THRDS_SLACK_BOT_TOKEN` — get notified when a thread goes out

`promote` posts **as you** (user token), and Slack doesn't notify you about your own messages — it marks them read as it posts them. So a successful prod post is, by default, completely silent: no unread, no push, nothing on your phone.

Set `THRDS_SLACK_BOT_TOKEN` to a bot token and `promote` will DM you a link to the post it just made. The DM comes from the app, not from you, which is what makes it a real notification.

Setup:

1. In your Slack app, add the **bot** scope `chat:write` (Bot Token Scopes), and reinstall.
2. **App Home → Show Tabs → enable the Messages tab.** Easy to miss, and without it your app has no DM surface to write to.
3. `export THRDS_SLACK_BOT_TOKEN=xoxb-…`

You don't need to open a DM with the app first, and you don't need `im:write` — passing your own `U…` id as the channel opens the conversation implicitly.

If the token isn't set, `promote` says so and carries on. If the DM fails, it warns but the post still succeeded — a notification problem never fails the thing it's reporting on.

### `THRDS_NO_PUSH`

Every state-mutating verb auto-commits **and pushes** to the session's gist. Set `THRDS_NO_PUSH=1` to keep the local commits and skip every push:

```bash
THRDS_NO_PUSH=1 thrds slack migrate    # local commit only; gist untouched
```

Useful when exercising a real session's code paths without writing to its gist — including from a *copy* of a session dir, which carries `.git` and its remotes along with it, so a copy is not by itself an isolated sandbox. The skip is announced on stderr rather than silent.

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
