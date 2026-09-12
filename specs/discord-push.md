# Spec: Discord push — bot-token CLI verbs + webhook-execution transport

Discord is the one platform where thrds's library is far ahead of its CLI. This spec closes that gap in two independent tracks — a **bot-token push** exposed properly in the `thrds discord` verb tree (the certain part), and a **webhook-execution transport** for per-message sender identity (the exploratory part) — and records the live prior art we should be converging with.

## Where we actually are (correcting the record)

The earlier framing that "Discord is paste-only because self-bots are banned" is true only of the **CLI surface**, not the library — but the library is less finished than a first read suggests:

- [`DiscordClient`][discord.py] (`thrds/discord.py`) has all the bot-API **primitives**: `post`, `edit`, `delete`, `create_thread`, `list_messages`, plus `sync` / `sync_linked`. Token is auto-prefixed `Bot `. Present since the first commit ("declarative thread sync for Slack **and Discord**").
- **But `create_thread` is called nowhere in the sync flow.** `core.sync` (core.py:536–552), for a fresh thread, posts the OP and then sets `thread_id = <the OP's message id>` and posts replies against it. That is correct for Slack (`thread_ts` == the OP's ts, and posting with it threads the reply), but **wrong for Discord**: a message id is not a channel, so `POST /channels/{op_message_id}/messages` is invalid. Discord requires an explicit `create_thread` off the OP, then posting into the returned thread channel. So the end-to-end "OP → open a thread → post replies into it" flow — the thing the Marin bot actually does — is **not wired through thrds's sync today**. discord-agent hand-rolls it precisely because sync doesn't.
- The `thrds discord` CLI group exposes only `init / lint / open / preview / render` — render-then-paste. No `push` / `sync`.
- The paste-for-prod rule (`specs/done/discord-platform.md`) was a *product* decision for the "post **as me**" use case: Discord bans user-token automation, so there was no clean "post as the user." It does **not** apply when the sender is a bot by design — which is the common case (digests, reports, announcements).

So Part 1 is **wire Discord thread-creation into the push, then expose it** — not merely surface an already-working path.

### Live prior art: `Open-Athena/discord-agent` ("Marin Bot")

The "don't we already do this?" instinct is correct. [`discord-agent`][discord-agent] posts the **Marin weekly digest to `#marin-bot` fully automatically**: `summarize.py` posts an OP, `create_thread`s off it, posts the summary + detail chunks *into* the thread, then edits the summary back with real permalinks (`summarize.py:481–543`). That is a line-for-line reinvention of thrds's `DiscordClient.sync_linked` — bot-token, single "Marin Bot" identity, `suppress_embeds=True`. It carries its own private copies of `discord_post` / `discord_edit` / `discord_create_thread` (in both `discord_api.py` and `summarize.py`).

thrds and discord-agent are **already half-merged**: thrds vendors discord-agent's Discord-markdown preview renderer (discord-agent commit `8d2a8e9` "Standalone preview build target, for thrds to vendor"; thrds's `discord preview` uses that bundle). This is the same posture ghpr was in before thrds subsumed it.

**Decided: invert the ownership.** thrds owns the Discord transport; discord-agent consumes it. DA's `discord_api.py` posting helpers and `summarize.py`'s private copies of them are a strict subset of `DiscordClient`; once the thread-creation seam (below) makes `sync`/`sync_linked` do the real OP→thread→replies flow, DA imports `thrds.DiscordClient` + `sync_linked` and deletes both copies. The clean split: **thrds = transport + reconcile + preview-render; discord-agent = archive/fetch, DB build, LLM summarize, D1 sync** (none of which belong in thrds). The preview renderer already flows DA→thrds, so this completes a direction already begun. Part 1 is the prerequisite.

## Identity models — why "both" is the answer, not "pick one"

The two transports are not competing implementations of the same thing; they're exclusive on different axes, so we want both:

| | bot token (have it) | webhook execution (new) |
|---|---|---|
| per-message sender (name + avatar) | ❌ one bot identity | ✅ `username` / `avatar_url` per message |
| create a thread | ✅ `POST …/messages/{id}/threads` | ❌ webhooks can't create threads |
| post *into* an existing thread | ✅ | ✅ via `?thread_id=` |
| edit a message | ✅ `PATCH …/messages/{id}` | ✅ `PATCH /webhooks/{id}/{token}/messages/{id}` |
| reactions / delete / list | ✅ | partial (delete yes; list/reactions still need bot token) |

So: a digest whose *value* is a per-contributor avatar mosaic (the way the Slack Shape-C digest works) wants webhook execution; a threaded report posted under one bot identity wants the bot token. A future hybrid could open the thread with the bot token, then fan replies into it via the webhook's `?thread_id=` for per-sender avatars.

## Part 1 — expose bot-token push in the CLI tree (do now)

Bring `thrds discord` up to parity with the session-verb half of `thrds slack`, backed by the existing `DiscordClient`.

**Config resolution** (mirroring `THRDS_SLACK_BOT_TOKEN` and discord-agent's `_resolve` precedence — CLI flag → env → session state):

- Token: `THRDS_DISCORD_BOT_TOKEN` (never echoed; script-wrapper only per the token-handling rule).
- `channel_id`: `--channel` / `THRDS_DISCORD_CHANNEL` / state.
- `guild_id`: `--guild` / `THRDS_DISCORD_GUILD` / state (required for `sync_linked` permalinks).
- `thread_id`: persisted in state after first push (so re-push reconciles the same thread), like Slack's `prod_threads`.

**State.** Discord sessions need their own small config rather than borrowing Slack's PEC/channel-prefix machinery. Add Discord-relevant optional fields to `SessionState` (or a nested `discord:` block) — `channel_id`, `guild_id`, `thread_id`, `thread_name` — pruned-when-default by the existing slim serializer, so a fresh Slack session is unaffected. Platform guard stays `_load_state(expected_platform='discord')`.

**Thread-creation wiring (the actual work) — a one-line seam, not a fork.** The reconcile in `core.sync` is genuinely platform-neutral: it touches messages only through the four `ThreadClient` methods (`list_messages`/`post`/`edit`/`delete`), and there is no Slack-specific logic in it. Exactly **one** line is Slack-shaped — `core.py:552`, `thread_id = result_msg.id` after the OP post. Slack's thread handle *is* the OP's ts, so replies address it directly; Discord's thread is a separate channel that must be `create_thread`d off the OP.

The fix is a thin per-client seam, not a reconcile fork and not a duplicated standalone impl:

- Add a client method `open_thread(op_id, name) -> reply_target`. Default (protocol/Slack): returns `op_id` — **byte-identical to today, zero Slack regression**. `DiscordClient` overrides it to `create_thread(op_id, name)` and returns the new thread-channel id.
- `core.sync` calls the seam in place of the hardcoded line 552. This fixes `sync` *and* `sync_linked` for Discord in one move; Slack's path and tests are untouched.

Store the created thread's channel id in `discord_thread_id` so re-push reconciles the same thread. Slack behavior must be pinned by a before/after test asserting the OP-then-replies call sequence is unchanged.

**Sender-override: raise, don't drop.** `post()`'s `username`/`icon_url`/`icon_emoji` are protocol kwargs (so `core.sync` can pass `Msg` overrides uniformly). Discord's bot API can't honor them, so — per "loud error, not silent drop" — `DiscordClient.post` **raises** when any override is actually non-None (bare-`str` threads pass none, so normal pushes are unaffected), with a message pointing at the webhook client (Part 2). Remove the `_sender_warned` warn-once machinery. Bluesky's silent-ignore should raise for the same reason.

**Known gap (log, out of scope here):** `DiscordClient.list_messages` marks every type-0 message `editable=True`, so it doesn't distinguish foreign (human) replies the way Slack's `list_messages` does — a hazard for mixed-author Discord threads, to be closed before any Discord push runs against a channel real people post into.

**Verbs** (subset of Slack's that map cleanly; skip PEC-specific ones like `promote` / `adopt` / `migrate`):

- `push` — reconcile the session doc into the thread via `sync` (or `sync_linked` when the doc has linked-summary structure), creating the thread on first push and storing `thread_id`. `-n/--dry-run`, `--pace`, `--jitter`, `--suppress-embeds`, `--thread-name`.
- `pull` — `list_messages` → write thread back to the doc (round-trip parity with `slack pull`).
- `diff` — dry-run reconcile, print the plan.
- `thread` — dump the live thread (debug / assertion aid).
- Keep `render` / `preview` as the paste path; `push` is additive, not a replacement.

**Tests** (exact-equality / parsed-structure per the testing rules): a `FakeDiscordTransport` recording `(method, path, data)` calls; assert the push of a 2-message doc yields exactly `[create_message(OP), create_thread, create_message(reply)]`, dry-run yields the plan with zero writes, re-push of identical content is a no-op, edited content yields a scoped edit. Reuse the `sync` test harness where it already covers this.

## Part 2 — webhook-execution transport (explore / experiment)

Once the bot client *raises* on sender overrides (Part 1), the webhook client becomes the actual way to *do* per-message sender on Discord — the transport that legitimately honors `username`/`avatar_url`, rather than pretending the bot API can.

- New `DiscordWebhookClient(webhook_url, thread_id=None)`: `post` → `POST {webhook_url}?wait=true[&thread_id=…]` with `username` / `avatar_url` / `content` / `flags`; `edit` → `PATCH {webhook_url}/messages/{id}`; `delete` likewise. Same `_curl` + backoff + 429/`EditRateLimited` handling as `DiscordClient` (factor the shared HTTP core out into a base, so the two clients read as one transport with two identity models).
- A `Msg` with sender overrides syncs cleanly through this client where the same thread would raise through the bot client — the caller's choice of client *is* the choice of identity model, made explicit.
- **Thread interplay.** Webhook posts can't *open* a thread. For a threaded, per-sender digest: open the thread with the bot token, then post replies via the webhook with `?thread_id=<that thread>`. Prove this combination works before committing to it.
- **Experiment target.** `#marin-bot-dbg` (private; member list is the user + one other). Needs a channel webhook (Integrations → Webhooks); store as `THRDS_DISCORD_WEBHOOK` / a `gcs-discord-webhook-staging` secret — the URL is a secret, never printed. Channel + webhook creation is the user's to do.
- Deliverable of the exploration: a short findings note (does `?thread_id=` edit-back work? rate-limit shape? avatar caching?) folded back into this spec before any productionization.

## Sequencing

1. Part 1 (bot-token CLI push + state + tests) — unblocks discord-agent convergence and any bot-identity report.
2. Decide subsumption vs. dependency for discord-agent (user).
3. Part 2 webhook experiment against `#marin-bot-dbg` once the user provisions the webhook.

Non-goals: reactions push, DM support, and the discord-agent archive/summarize/D1 logic (stays in that repo regardless of subsumption outcome).

[discord.py]: ../thrds/discord.py
[discord-agent]: https://github.com/Open-Athena/discord-agent
