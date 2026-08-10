# Spec: per-message custom sender (name + avatar)

**Status:** baseline implemented 2026-08-10; "aggressive sender-change reposts" (see §Aggressive-mode extension) still open. Motivated by dogfooding in the OA **GCS-usage daily digest** (`marin-gcs-usage`), where a thread wants *different* senders on its OP vs. its replies, and a **custom hosted avatar** (`icon_url`) that thrds can't set at all today.

## Divergences from this draft when implemented

- **Type named `Msg`, not `Post`.** `Post` would re-introduce the naming clash we deliberately renamed away from earlier (`Post` → `Doc`, to avoid "OP = original post" confusion). `Msg` sits cleanly alongside `Message` (live/returned) and `DocMessage` (in-Doc).
- **Icon resolution is per-icon-unit, not per-field.** The spec's `msg.icon_url or self.icon_url; msg.icon_emoji or self.icon_emoji; then xor url over emoji` has a subtle bug: with client `icon_url='default.png'` + msg `icon_emoji=':new:'`, the field-by-field version keeps the client's icon_url, silently ignoring the msg's explicit override. Implementation treats icon as a unit at the msg level — if the msg sets **either** icon field, the client's icon (whichever flavor) is fully replaced. Within either source, `icon_url` still beats `icon_emoji` when both set. Matches implicit user intent; test-covered by `test_slack_post_msg_icon_emoji_overrides_client_icon_url`.

## Motivation

The GCS-usage digest is one Slack thread per day:

- **OP** — sender name `GCS usage — 2026-08-10` (the date doubles as the thread's header), custom avatar (a hosted composite PNG at `gcs-usage-icons.pages.dev/gcs-digest.png`).
- **Replies** (`Teams:` / `Buckets:` breakdowns) — sender name just `GCS usage` (no date; the OP already carries it), same custom avatar.

thrds can't express this today:

1. **No `icon_url`.** `SlackClient` supports client-level `icon_emoji` only. Custom *hosted* avatars (Slack's `icon_url`, needs the `chat:write.customize` scope) have no path.
2. **Sender is client-level, not per-message.** `username`/`icon_emoji` live on the client and apply uniformly to every `post()`, so OP-vs-reply name differences are impossible in one `sync()`.

The workaround we ran (see `marin-gcs-usage` `tmp/thread-sync.py`): post the OP out-of-band via raw `chat.postMessage` (to get `icon_url` + the date username), then run `sync(thread_ts=…)` for the replies with a *second* client whose `username="GCS usage"`, and hand-`chat.delete` the replies whenever the name needed to change. That's exactly the manual bookkeeping thrds exists to remove.

## Current state

- `SlackClient.__init__(token, channel, username=None, icon_emoji=None)` — `slack.py:90`.
- `post(content, thread_id=None)` applies `self.username` / `self.icon_emoji` to every message — `slack.py:205-208`.
- `edit(message_id, content)` is `chat.update` with `text` only — `slack.py:219-236`.
- `Thread(messages: list[str])` — desired state is bare content strings (`core.py`).
- `sync()` diffs by position: delete extras, edit-or-skip overlap, post new (`core.py:120`).

## The load-bearing constraint

**`chat.update` cannot change a message's `username` or icon** — both are fixed at post time (Slack keeps the original on edit; the API silently ignores `username`/`icon_url`/`icon_emoji` in an update payload). So:

- Per-message sender is a **post-time attribute**. It applies on `POST`; content edits (`EDIT`) leave the live sender untouched (which is the *desired* behavior — a daily-number tweak shouldn't churn the avatar).
- Changing a message's sender *without* changing its content therefore requires a **delete + repost**, not an edit. This mirrors the Bluesky client's existing "no edit → delete+repost" fallback.

The baseline design below treats sender as post-time-only (set on post, preserved on edit) and does **not** auto-detect sender drift. That covers the digest use case fully and keeps `sync`'s diff logic unchanged. Auto-reposting on sender drift is a documented future extension (see Out of scope).

## Proposed API

### Per-message input type

Add an optional richer desired-message type; keep bare `str` working:

```python
@dataclass
class Post:
    """A desired message with an optional per-message sender override.

    Bare `str` entries in `Thread.messages` keep using the client defaults;
    a `Post` overrides any of name/avatar for that one message, falling back
    to the client-level default for fields left None.
    """
    content: str
    username: str | None = None
    icon_url: str | None = None
    icon_emoji: str | None = None

@dataclass
class Thread:
    messages: list[str | Post]
```

`sync`'s positional diff compares on **content only** — normalize each desired entry to its `.content` for the existing-vs-desired comparison, and carry the sender fields through to `post()`. (An `EDIT` uses `content`; the sender fields are ignored on edit, per the constraint above.)

### Client-level `icon_url`

Add `icon_url` alongside `icon_emoji` as a thread-wide default:

```python
def __init__(self, token, channel, username=None, icon_url=None, icon_emoji=None):
```

### Sender resolution in `post()`

Resolve per message: message override → client default → unset. Slack takes `icon_url` **xor** `icon_emoji`; if both resolve, `icon_url` wins.

```python
username  = msg.username  or self.username
icon_url  = msg.icon_url  or self.icon_url
icon_emoji= msg.icon_emoji or self.icon_emoji
if username:   data["username"]   = username
if icon_url:   data["icon_url"]   = icon_url      # wins over emoji
elif icon_emoji: data["icon_emoji"] = icon_emoji
```

### Resulting call site (the digest)

```python
client = SlackClient(token, channel, icon_url=AVATAR)  # avatar is the thread default
thread = Thread(messages=[
    Post(op_text, username=f"GCS usage — {date}"),  # OP: date in the name
    Post(teams_text, username="GCS usage"),          # replies: plain name
    Post(buckets_text, username="GCS usage"),
])
client.sync(thread, thread_ts=op_ts)
```

## Platform notes

- **Slack** — primary target. `chat.postMessage` accepts `username` + `icon_url`/`icon_emoji` with the `chat:write.customize` scope. Implement fully.
- **Discord** — the **bot API cannot** override name/avatar per message; only *webhook execution* (`username` + `avatar_url`) can. Out of scope here: `DiscordClient` (bot token) should **ignore** per-message sender fields with a one-time warning rather than silently promise something it can't do. A webhook-backed sender is a separate spec if wanted.
- **Bluesky** — no sender-override concept; ignore the fields.

## Backwards compatibility

- `Thread.messages: list[str]` callers are unaffected — `str` entries use client defaults exactly as today.
- Existing client-level `username` / `icon_emoji` behavior is unchanged; `icon_url` is additive.

## Testing

- `post()` payload assertions (mock `_request`): message-level override beats client default; `icon_url` beats `icon_emoji` when both set; `str` entry falls back to client defaults; no sender fields sent when none resolve.
- `sync()` with mixed `str` / `Post`: positional diff still keys on content; an `EDIT` carries `content` only (no sender in the update payload); a `POST` carries the resolved sender.
- Follow the repo's exact-equality assertion style (assert the full built payload dict, not membership).

## TypeScript parity

The `ts` branch (Slack subset) should mirror: a `Post` type, client `iconUrl`, and the same resolution/precedence. The cross-language `tests/fixtures/sync.json` contract is **unaffected** — it encodes the content diff (post/edit/delete/skip), and sender is post-time metadata outside that diff. Track as a follow-up; note it in the TS port's tracking spec.

## Out of scope (future)

- **Discord webhook sender** — per-message name/avatar via webhook execution instead of the bot API.

## Aggressive-mode extension: sender-change reposts (in-progress)

Baseline treats sender as post-time-only. The extension: allow `sync` to change an existing message's sender by **delete + repost**, gated behind policy. Motivated by the "I want to fix the sender on a fresh reply" ergonomic — real, but tangled with reactions loss + notification pings + Slack's message ordering.

### Slack constraints (verified)

- `chat.update` silently ignores `username`/`icon_url`/`icon_emoji`. Only path to change sender is delete + repost.
- Reactions on the deleted message are lost — no API to preserve them.
- A reposted message gets a fresh (later) `ts`; it moves to the END of the thread's chronological order.
- `thread_ts` is fixed at OP-post time. Deleting the OP + reposting creates a **new thread** (new `thread_ts`); anything referring to the old OP's ts is dead.
- Foreign (non-editable) messages between the change target and thread-end cannot be deleted, so their relative position is fixed — meaning any cascade that would move ours-messages past a foreign one is a semantic hazard (foreign messages responding to the pre-cascade thread ordering suddenly appear out of context).

### Design (settled 2026-08-10)

**Hard rules (not gated):**

- **OP sender change is always forbidden.** Delete+repost of the OP changes `thread_ts`, invalidating `thrds.json` state, cross-refs from other threads, and any external permalinks. If you want a different OP sender, that's a new thread (`thrds init` + repost + archive old), not a `sync()`.
- **Cascade cannot cross a foreign message.** If any foreign reply sits between the sender-change target and thread-end, hard-abort. No knob to override — there's no way to preserve order across a message you can't delete.

**Opt-in via `SyncOptions.sender_change`:**

```python
@dataclass
class SenderChangePolicy:
    """Policy for handling a per-reply sender mismatch on existing messages.

    Default (SyncOptions.sender_change = None): any sender mismatch raises
    `SenderChangeForbidden`. Setting a policy opts in to delete+repost with
    the safety gates below.
    """
    max_reposts: int = 3           # cascade length cap (target + later ours-replies to preserve order)
    lose_reactions_ok: bool = False  # if False, abort on any target with reactions

@dataclass
class SyncOptions:
    ...
    sender_change: SenderChangePolicy | None = None
```

**Detection**: extend `Message` with optional sender fields:

```python
@dataclass
class Message:
    id: str
    content: str
    editable: bool = True
    # NEW — None means "unknown / not populated by this client".
    sender_username: str | None = None
    sender_icon_url: str | None = None
    sender_icon_emoji: str | None = None
```

`SlackClient.list_messages` populates these from the returned message dict; other clients (Discord bot, Bluesky) leave them `None` — the aggressive mode is Slack-specific.

**Sync trigger**: only at the SKIP path (content matches). If `desired[i] is Msg` AND its resolved sender (msg → client → unset, matching `post()`'s logic) differs from `existing[i]`'s stored sender:

1. If `i == 0` (OP) → raise `SenderChangeForbidden` (hard rule).
2. If any foreign message sits at index > i in the thread → raise `SenderChangeForbidden` (hard rule).
3. Plan cascade = [i, i+1, ..., last-ours-index] (contiguous ours-replies from target to thread-end).
4. If `len(cascade) > policy.max_reposts` → raise.
5. If `not policy.lose_reactions_ok`: pre-flight `reactions.get` for each cascade member. If any has reactions → raise (listing which).
6. Execute: delete cascade end-to-start; repost start-to-end (each with its original content + preserved sender, except the target which gets its new sender).

**Content-change cases**: if content ALSO differs from live, prefer plain `chat.update` (existing EDIT path) and leave sender drift silent. Rationale: v1 keeps the sender-cascade concern purely at the SKIP boundary, avoids interleaving EDIT + DELETE+REPOST cost models. Users needing "changed content + changed sender to also propagate" can re-run sync after the edit lands — the second pass will hit the SKIP-mismatch path and cascade.

### Rejected knobs

- `allow_op_sender_change` — hard rule instead. `thread_ts` change breaks too much.
- `across_foreign_ok` — no way to actually preserve order past a foreign msg.
- Channel-member-count-based rate limiting — poor proxy for notification spam (users have per-channel prefs); `lose_reactions_ok=False` implicitly covers the "nobody has engaged yet" case.

### v1 status: implemented 2026-08-10

Landed on `py` alongside the baseline. Full suite: 350 passed, 2 skipped.

**One documented limitation**: `SlackClient.list_messages` populates
`sender_icon_url` from `icons.image_*` but Slack does not preserve the
`icon_url`-vs-`icon_emoji` distinction on read (emojis are rendered to
URLs server-side). So `_sender_mismatch` skips the icon check entirely
when the desired `Msg` uses `icon_emoji` (not `icon_url`) — users on
emoji-based overrides don't get auto-cascade. Rare in practice; the
motivating digest use case is `icon_url`-based (hosted avatar).
