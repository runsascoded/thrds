# thrds

Declarative thread sync for Slack, Discord, Bluesky, and GitHub (PRs/issues).

Two layers:

- **[CLI](#cli)** — draft threads/posts/PRs as local markdown, one session per doc in a private git repo mirrored to a secret gist, then sync or publish per platform: [`thrds slack`](#slack--thrds-slack--slck) (a.k.a. `slck`), [`thrds github`](#github--thrds-github--ghpr) (a.k.a. `ghpr`), [`thrds discord`](#discord--thrds-discord), [`thrds bsky`](#bluesky--thrds-bsky), [`thrds capture`](#capture--thrds-capture).
- **[Library](#library)** — `SlackClient` / `DiscordClient` / `BskyClient` take a desired thread state (list of message contents), diff against existing messages, and apply minimal edits/posts/deletes to converge.

> A TypeScript port (Slack subset) lives on the [`ts` branch][ts-branch], published on npm as [`@rdub/thrds`][npm]; both share `tests/fixtures/sync.json` as the cross-language contract for the sync algorithm.

## Install

```bash
pip install thrds            # Core (zero deps); installs `thrds`, `slck`, `ghpr`
pip install thrds[bsky]      # + Bluesky (atproto)
```

Slack and Discord clients use only stdlib (`urllib`) and a `curl` subprocess respectively.

## CLI

One session per `.md` file, living in `<git-root-or-cwd>/<platform>/<slug>/` with its own private git repo and (by default) a secret gist mirror. Every state-mutating verb auto-commits and pushes to the gist, so the doc's iteration history is the artifact you keep.

Every platform group shares one sync model, borrowed wholesale from git: a remote-tracking ref per remote under `refs/remotes/<name>`, `fetch` to observe it, `pull -m rebase|merge|overwrite` to reconcile, and a `push` that refuses a non-fast-forward. See [`specs/remotes-model.md`](specs/remotes-model.md).

Each session's `platform` is stamped into `thrds.yml` at init and guarded on every verb — `thrds slack push` inside a capture session errors immediately rather than failing halfway.

### Slack — `thrds slack` / `slck`

Draft multi-thread Slack posts locally, sync to a staging private channel, promote each thread to its real prod destination. `slck` *is* the `thrds slack` group (same click group object), so every verb, flag, and help string is shared.

```bash
slck init draft.md                     # scaffold session dir + gist mirror
slck migrate                           # split the doc into per-thread NN-slug.md files
slck push [-r <remote>]                # commit, then sync every thread to staging (refuses if it moved; -f)
slck pull                              # fetch + reconcile; local commits survive (-m rebase|merge|overwrite)
slck fetch [staging|prod]...           # record Slack's state in staging/prod/upstream refs (default: all)
slck diff [<slug>]                     # local vs. Slack per thread; classified once fetched
slck status                            # per-thread state + resolved destination
slck promote cw-mpu                    # post ONE thread to its target (plan + confirm; re-promotes gated)
slck drop cw-summary                   # mark a thread abandoned without posting
slck reopen cw-mpu                     # un-finalize a posted/dropped thread
slck reorder                           # renumber thread files to a gapless 01..NN
slck archive                           # archive staging (once all threads are terminal)
slck list-sessions #foo                # what thrds sessions exist in #foo
slck recover -i <sid> #foo             # rebuild a lost session from Slack metadata
slck replay [-n]                       # rewrite a pre-migrate session's *git history* to per-thread files
```

**One file per thread.** A session directory holds `01-slug.md`, `02-slug.md`, … — one Slack thread each (`+++` separates replies *within* a thread), so per-file git history reads as "*this message* went v2→v3" rather than "the doc changed". `migrate` converts a whole-doc session's working tree; `replay` converts its git history too (verifying every rewritten commit round-trips before writing, to branch `per-thread`, never force-pushing). `reorder` renumbers files gaplessly — `thrds.yml` is keyed by slug, so targets and posted timestamps follow their thread, and `git log --follow` survives the rename.

**Destination is a property of the thread, not the session.** Each thread records its own `{channel, thread_ts?}` target in `thrds.yml` (session-level `prod_channel` is just the default). No `thread_ts` → post a new top-level message; with one → the thread's messages go in as replies — so "draft a reply to someone else's post" and "batch six messages into one channel" are the same mechanism.

**Staging-only chrome.** Staged messages carry one extra line — where the draft is aimed, what it became once posted, and which gist file it is:

```
→ #oa-amazon-trainium · 01-mfu.md
→ (#marin-alerts) · 02-cw-summary.md      # arrow links to the message being replied to
✅ #marin-alerts · 02-cw-summary.md       # posted: condenses to our permalink
```

It's also an *input*: edit the line in Slack and the next `pull` acts on it —

| write | effect |
| --- | --- |
| `→ #some-channel` | retarget the thread |
| `→ <paste a channel link>` | same |
| `→ <paste a message link>` | aim it *into* that thread |
| `→ #chan · 04-idea.md` | rename/renumber the thread's file |

You can even start a thread by writing one in the staging channel: a top-level message with a chrome line is adopted on the next `pull` (file created, target recorded); one without stays a note to self. Chrome is configured in `thrds.yml` (`staging_chrome`), appended after md→mrkdwn conversion and stripped before the reverse, and `promote` refuses to post a body still carrying one — publishing a secret-gist URL to a real channel is worth failing closed over.

**Finalizing.** Once a thread is `posted` or `dropped`, its staged copy re-renders with chrome in a `context` block — Slack strips the Edit affordance from messages carrying blocks, so the staged copy visibly locks. `reopen` moves it back to `draft` (keeping `posted_ts`, so a later `promote` syncs the existing message rather than double-posting). `finalize_terminal: false` disables.

**`fetch` gives `diff` a merge base.** Slack keeps no version of itself, so `fetch` keeps one for it: `refs/remotes/{staging,prod}` plus merge base `refs/heads/upstream`, each pointing at a commit whose tree is what Slack last projected. That turns `diff`'s two-way delta into a classification — *changed in Slack* (`pull` applies it), *changed locally* (`push` sends it), or *CONFLICT* (both moved) — distinctions a two-way diff structurally can't make. The refs are plain git: `git show upstream`, `git diff upstream HEAD`, `git diff staging prod` all mean what you'd hope. (`pull -n` is the raw dump of Slack's current state, no comparison.)

**Prod push is per-thread, never whole-doc.** `promote` resolves the destination, prints it with the exact body, asks, then posts only that thread. `archive` is its own verb, refusing until every thread is `posted` or `dropped` (`-f` overrides).

**CRUD verbs** for ad-hoc operations — a first-class alternative to hand-rolled `chat.*` heredocs. These default to **raw mrkdwn** (send verbatim); `-m` opts into md→mrkdwn conversion (the opposite of the session verbs' default — see [`raw-mrkdwn-passthrough`][raw-spec]):

```bash
thrds slack history #foo -n 10           # last 10 messages (ts, sender, text)
thrds slack thread  #foo 1783.1          # OP + replies as a table (`-j` = JSON)
thrds slack rm      #foo 1783.1 1783.2   # delete msg(s); `-f` = orphans_ok
thrds slack post    #foo '*bold*'        # raw mrkdwn (`-m` = convert md first)
thrds slack post    #foo 'hi' -u 'Bot' -i https://cdn.example/a.png -t 1783.0
thrds slack edit    #foo 1783.1 'new'    # edit; raw by default
thrds slack permalink #foo 1783.1        # get workspace permalink URL
```

**Image blocks.** A trailing standalone `![alt](url)` line becomes a Block Kit `image` block after the message body — and because an image *block*'s URL (unlike an attached file) can be edited, a job can converge the same OP with a new card URL and the image refreshes in place. If the bytes change under a stable URL, suffix `{bust}`: each post/edit appends a fresh `?thrds_bust=<minute>` param so Slack's URL-keyed cache refetches, while a no-op converge never touches it. See [`specs/done/editable-image-blocks.md`](specs/done/editable-image-blocks.md).

#### Tokens + scopes

The CLI reads the Slack token from `THRDS_SLACK_TOKEN` (deprecated alias `SLACK_THRDS_USER_TOKEN` still works, with a warning).

- **User token** (`xoxp-…`) — needed for the **session verbs** (`init`/`push`/`pull`/`diff`/`archive`/`list-sessions`/`recover`/`open`): staged posts are your Slack user's own, and only a token you own can `chat.update` them.
- **Bot token** (`xoxb-…`) — sufficient for the **CRUD verbs** and for programmatic `sync()`/`sync_linked()` when the bot owns the content lifecycle end-to-end.

Add scopes under **OAuth & Permissions** (User Token Scopes vs. Bot Token Scopes; names are identical):

| Scope | Needed for | Session verbs | CRUD / `sync()` |
| --- | --- | :---: | :---: |
| `chat:write` | Post / edit / delete messages | ✓ | ✓ |
| `groups:write` | Create + archive staging PCs | ✓ (`init`, `push`, `archive`) | — |
| `groups:read` | Read + resolve `#name` for private channels | ✓ | ✓ |
| `channels:read` | Resolve `#name` for public channels | ✓ (if pushing/pulling public) | ✓ (public channels) |
| `users:read` | Resolve foreign-author names on `pull` | ✓ (`pull`) | — |
| `emoji:read` | Download custom workspace emoji on `pull` | ✓ (`pull`) | — |
| `chat:write.customize` | Per-message `username` / `icon_*` (bot token only — `post` errors under a user token) | If used | If used |
| `reactions:read` | `SenderChangePolicy` pre-flight (library) | — | If using aggressive-mode `sync` |

Metadata visibility is app-scoped, so `recover` needs no additional scope.

#### `THRDS_SLACK_BOT_TOKEN` — get notified when a thread goes out

`promote` posts **as you**, and Slack marks your own messages read as it posts them — so a prod post is silent by default. Set `THRDS_SLACK_BOT_TOKEN` to a bot token and `promote` DMs you a link to the post it just made (from the app, which is what makes it a real notification). Setup: add bot scope `chat:write` + reinstall, and enable **App Home → Show Tabs → Messages tab** (no `im:write` needed — passing your `U…` id as the channel opens the DM implicitly). Unset → `promote` says so and carries on; DM failure warns but never fails the post it's reporting on.

### GitHub — `thrds github` / `ghpr`

"Clone" a GitHub PR or issue — title, description, comments, review threads — to local markdown files, edit them in your editor (full markdown, no textarea, no character limits), and push back, mirrored to a secret gist for history and sharing.

This was [`ghpr`][ghpr] (PyPI: [`ghpr-py`][ghpr-py]), a standalone tool that converged on the same sync model from the other direction; it now lives here as a platform adapter ([`thrds/platforms/github/`](thrds/platforms/github/) — its [README](thrds/platforms/github/README.md) has the full docs). Installing `thrds` installs **`ghpr`**, which *is* the `thrds github` group — same arrangement as `slck`, so every existing `ghpr` invocation (and the `ghprc`/`ghprd`/`ghprp` aliases) keeps working.

```bash
ghpr clone owner/repo#123        # or a PR/issue URL, or bare inside a repo on a PR branch
ghpr diff                        # local vs GitHub, with ownership warnings for others' comments
ghpr fetch                       # snapshot GitHub into `refs/remotes/github`; touches nothing else
ghpr pull [-m rebase|merge|overwrite]
ghpr push                        # send committed local state to GitHub + gist; refuses a non-FF
ghpr sync                        # pull, then push
ghpr show / ghpr open            # print / browse the PR + gist URLs
ghpr review …                    # local ops on review threads (synced on next push)
ghpr init / ghpr create          # draft a NEW PR/issue in gh/drafts/<slug>/, then file it
ghpr ingest-attachments          # rewrite user-attachment URLs to durable gist permalinks
```

**Clone layout.** Clones live in `gh/<number>/` (the platform's conventional short form — the dir isn't named for the tool): `repo#123.md` holds title + description, each comment is its own `zNNNNNN-<author>.md`, review threads get their own files. Each clone is a git repo with `refs/remotes/github` as GitHub's tracking ref — reflog and all — so `git diff HEAD github` is "what changed on GitHub", and fetch/pull/push mean exactly what they mean in git.

**Drafts.** A `new*.md` file posts as a new comment on the next `push`; review-thread reply drafts post only once *committed*, so you can stage several and publish selectively. `ghpr init <slug>` scaffolds a whole new PR/issue under `gh/drafts/<slug>/` and `ghpr create` files it (the dir then becomes `gh/<number>/`).

### Discord — `thrds discord`

Capture + MD-compat lint for Discord. Prod delivery is copy-paste (self-bots are ToS-prohibited), so there's no `push`; `render` prints the doc to stdout and auto-runs `lint` (flags tables and raw `@name` — constructs Discord's user-message renderer drops; masked `[text](url)` links do render):

```bash
thrds discord init draft.md      # scaffold session dir + gist (no channel/bot)
thrds discord lint               # just the MD-compat warnings
thrds discord render | pbcopy    # MD → clipboard (warnings → stderr)
thrds discord open               # browse the gist
thrds discord preview            # Discord-faithful live preview + edit loop
```

`preview` serves the doc at `localhost:3077`, rendered through Discord's real markdown semantics (a vendored, prebuilt bundle of the `discord-agent` parser/renderer — no node needed at install time; `scripts/sync-preview-bundle` refreshes it, `thrds/preview/BUNDLE_PROVENANCE` records the source commit). Edits in the page save back to the `.md` (mtime-guarded, conflict banner on races); `-c` commits each save, so UI iterations land in the gist trajectory like any other edit.

### Bluesky — `thrds bsky`

Same shape, different lints: bsky's chief drafting pain is the **300-char post limit** (per paragraph), so `lint` flags oversized paragraphs, and warns on masked links (bsky auto-linkifies bare URLs via facets; `[text](url)` renders literally):

```bash
thrds bsky init draft.md         # scaffold session dir + gist
thrds bsky lint                  # length + link warnings
thrds bsky render | pbcopy       # MD → clipboard
thrds bsky open                  # browse the gist
```

### Capture — `thrds capture`

Capture-only sessions: same on-disk shape (git repo + gist mirror), no platform target — for capturing the iteration history of a post you'll paste somewhere `thrds` doesn't (yet) integrate with. The minimal flow is two commands → a secret, one-file, two-revision gist:

```bash
dir=$(echo "$claude_draft" | thrds capture init)   # secret gist, revision 1; session dir → stdout
echo "$final_msg" | thrds capture update "$dir"    # revision 2 (unchanged content = no-op)
```

`init` takes stdin (slug derived from the first line, or `-s`) or a `DOC_PATH`; `update` replaces the doc from stdin and pushes one revision (`SESSION_DIR` defaults to the CWD). For hand-editing sessions:

```bash
thrds capture init draft.md      # scaffold session dir + gist mirror (no channel)
thrds capture push               # commit doc changes and push to the gist
thrds capture open               # browse the gist
```

Capture gists hold **only the doc**: `thrds.yml` is a local, git-excluded file (and, like every fresh `thrds.yml`, holds only non-default fields — ~4 lines).

### `THRDS_NO_PUSH`

Every state-mutating verb auto-commits **and pushes** to the session's gist. `THRDS_NO_PUSH=1` keeps the local commits and skips every push (announced on stderr, never silent) — useful when exercising a real session's code paths without writing to its gist, including from a *copy* of a session dir, which carries `.git` and its remotes along and so is not by itself an isolated sandbox.

## Library

```python
from thrds import SlackClient, Thread

slack = SlackClient(token="xoxb-...", channel="C0AQC2VKEJF")
thread = Thread(messages=["OP text", "Reply 1", "Reply 2"])

result = slack.sync(thread)                                  # create new thread
result = slack.sync(thread, thread_ts="1775516040.743629")   # converge existing one
```

```python
from thrds import DiscordClient, BskyClient

discord = DiscordClient(token="bot-token", channel_id="1489279547689140505")
result = discord.sync(thread, thread_id="1490821926288097503")

bsky = BskyClient(handle="you.bsky.social", password="app-password")
result = bsky.sync(thread)   # no edit API: falls back to delete+repost on change
```

### Sync algorithm

Given desired messages `M` and existing thread messages `N`:

1. **Delete** extras from the end (backwards — replies before OP)
2. **Edit** overlapping messages where content changed (skip unchanged)
3. **Post** new messages at the end

Foreign (non-editable) messages — e.g. human replies in a bot thread — are skipped: sync only touches the bot's own messages.

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
result = discord.sync_linked(linked, thread_id="...", guild_id="...")  # [**Title**](url)
result = slack.sync_linked(linked, thread_ts="...")                    # <url|*Title*>
```

Two-phase: posts all messages with placeholder links, then edits summaries with real links once message IDs are known.

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

### Features

- **Foreign message preservation**: non-bot messages in threads are skipped during sync (no `cant_update_message` errors)
- **Rate limit handling**: Slack 429 retry with `Retry-After`; configurable `pace` + `jitter`; Discord 30046 (edit limit on old messages) falls back to delete+repost
- **Orphan guard**: Slack `delete()` raises `OrphanedRepliesError` rather than orphaning thread replies
- **Unfurl/embed suppression**: Slack link previews and Discord embeds suppressed via options
- **Editable image blocks (Slack)**: trailing `![alt](url)` → Block Kit `image` block whose URL swaps on `chat.update`; `{bust}` for cache-busting (see [above](#slack--thrds-slack--slck))
- **Sender-override guard**: `username`/`icon_*` customization only works on bot tokens; `post` fails closed under a user (`xoxp-`) token instead of letting Slack silently drop the fields
- **Discord niceties**: system messages filtered from `list_messages`; `Bot ` token prefix auto-prepended
- **Metadata support**: Slack message metadata passthrough

### Types

```python
@dataclass
class SyncResult:
    thread_id: str          # thread_ts (Slack), thread channel ID (Discord), AT URI (Bluesky)
    message_ids: list[str]  # Per-message IDs
    actions: list[Action]   # What was done: Skip, Edit, Post, Delete

@dataclass
class Action:
    type: ActionType            # SKIP, EDIT, POST, DELETE
    index: int
    message_id: str | None
    content: str | None         # Desired text (POST, EDIT, SKIP)
    prior_content: str | None   # Previous text (EDIT, DELETE)
```

| `SyncOptions` | Default | Description |
|--------|---------|-------------|
| `dry_run` | `False` | Print actions without executing |
| `pace` | `0.0` | Seconds between mutating API calls |
| `jitter` | `0.0` | Random additional delay (0 to `jitter`) added to `pace` |
| `suppress_embeds` | `False` | Discord: suppress link previews |
| `suppress_unfurls` | `True` | Slack: suppress link previews |

## Used by

- [hudcostreets/nj-crashes] — Slack crash-notification threads (`SlackClient.sync()`)
- [Open-Athena/marin-discord] — Discord summary threads (`DiscordClient.sync_linked()`)

[ghpr]: https://github.com/runsascoded/ghpr
[ghpr-py]: https://pypi.org/project/ghpr-py/
[raw-spec]: specs/done/raw-mrkdwn-passthrough.md
[hudcostreets/nj-crashes]: https://github.com/hudcostreets/nj-crashes
[Open-Athena/marin-discord]: https://github.com/Open-Athena/marin-discord
[ts-branch]: https://github.com/runsascoded/thrds/tree/ts
[npm]: https://www.npmjs.com/package/@rdub/thrds
