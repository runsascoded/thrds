# Spec: per-message sender in the thread doc (versioned name + avatar)

Bring per-message sender (display name + avatar) into the **versioned `.md`** thread format, so a bot's *presentation* — who each message appears to be from — is authored and iterated in the gist trajectory like its content, not just built programmatically at send time.

## Motivation

Per-message sender already exists as a **programmatic** API: `Msg(content, username=, icon_url=, icon_emoji=)`, resolved by `core.sync` (`specs/per-message-sender.md`), honored natively by `SlackClient` (`chat:write.customize`) and, for Discord, by `DiscordHybridClient` (bot owns the thread/OP, webhook fans per-sender replies — `specs/discord-push.md`). What's missing is a way to say it in the **doc**: today `thrds {slack,discord} push <doc.md>` only carries bare content strings, so a per-sender digest can only be built in app code, and its sender choices never land in version control. This adds the doc syntax and wires `push` to it — cross-platform (helps Slack authoring too), motivated by wanting to iterate on a bot's presentation in the versioned doc.

## The format today (what we build on)

`thrds/md.py`, per-thread-file layout (the one `push` uses — `parse_thread` / `serialize_thread`):

- Frontmatter: a deliberately minimal `key: value` string-scalar parser (no nesting, no quoting), known keys `channel` / `thread_ts` / `session_id`. **Not** switched to `yaml.safe_load`: it would coerce `thread_ts: 1775516040.743629` to a float and lose the string guarantee.
- Body: the OP is the text before the first `+++`; each `+++` starts a reply. `+++ @author` marks a **foreign** reply (someone else's — `DocMessage.author`, preserved by sync, never touched). The OP has **no delimiter line**.

Two hard constraints from this:
1. **Don't overload `+++ @author`** — foreign (preserve) is the opposite of "post as sender X" (ours, custom identity). Custom sender needs a distinct token; `@author` keeps its meaning.
2. **The OP has no delimiter** in the per-thread layout, so its sender can't be inline — it rides in frontmatter.

## Syntax (Option 1: named senders in frontmatter, referenced with `as`)

Named sender profiles are declared once in frontmatter (flat dotted keys — string-safe, fits the existing parser, DRY for a repeated avatar, echoing the link-footer style), then referenced by short name. The OP references via a frontmatter key; replies reference inline on their `+++`.

```markdown
---
channel: C0AQC2VKEJF
op_sender: gcs-op
sender.gcs-op.name: GCS usage — 2026-09-14
sender.gcs-op.avatar: https://gcs-usage.pages.dev/composite.png
sender.gcs.name: GCS usage
sender.gcs.avatar: https://gcs-usage.pages.dev/composite.png
---
Daily GCS usage summary.

+++ as gcs
Teams: …

+++ as gcs
Buckets: …
```

- **Sender defs**: `sender.<name>.name` → `username`; `sender.<name>.avatar` → `icon_emoji` if the value is `:emoji:` (matches `^:[\w+-]+:$`), else `icon_url` (icon-as-a-unit, matching the `Msg` resolution). Either field may be omitted (name-only or avatar-only), but a profile must set at least one. `<name>` is `[a-zA-Z0-9_-]+`.
- **OP sender**: `op_sender: <name>` frontmatter key (the OP has no delimiter). Optional; absent = default identity.
- **Reply sender**: `+++ as <name>` (our reply, custom identity). Bare `+++` = default identity (unchanged). `+++ @author` = foreign (unchanged). `as` and `@author` on the same delimiter is an error (a foreign message isn't ours to re-sender).
- **Validation**: an `as` / `op_sender` ref not in the `sender.*` map → error naming the ref; a `sender.<name>.<field>` with unknown `<field>` → error.

Rejected alternatives: nested `senders:` YAML map (the `thread_ts` float coercion above); inline `+++ name="…" avatar=…` (repeats long avatar URLs, noisy for the repeated-avatar digest). `=== slug as <name>` for the *legacy* multi-thread layout (`parse_doc`) is out of scope — that layout is retained only for `migrate`.

## Data model (`thrds/doc.py`)

- `DocMessage` gains `sender: str | None = None` — the profile *ref name* for a reply (kept as the ref, not expanded, so serialize round-trips `+++ as gcs`). OP's ref is `Frontmatter.op_sender`, not `messages[0].sender` (single source of truth per the no-delimiter reality).
- New `@dataclass SenderProfile: name: str | None; icon_url: str | None; icon_emoji: str | None`.
- `Frontmatter` gains `op_sender: str | None` and `senders: dict[str, SenderProfile]` (default empty).

## Doc → `Thread` resolution (shared helper)

A new `resolve_messages(thread: DocThread, fm: Frontmatter) -> list[str | Msg]`:

- index 0 uses `fm.op_sender`; index i>0 uses `messages[i].sender`; foreign messages (author set) are excluded from the push exactly as today.
- ref present → `Msg(content, username=prof.name, icon_url=prof.icon_url, icon_emoji=prof.icon_emoji)`; ref absent → bare `content` (so a doc with no senders produces the exact same `list[str]` as today — zero behavior change).

`slack push` and `discord push` both call it instead of `[m.content for m in …]`.

## CLI wiring (`thrds/cli.py`)

- **`slack push` / `slack promote`** (done): the read helpers (`read_thread` / `read_threads` / `find_thread`) now return the whole `ParsedThread` (thread **+ frontmatter**) instead of a bare `DocThread`, so the sender profiles survive to the sync sites. `sync_threads_staging` takes a `frontmatter_by_slug` map and `promote_thread` a `frontmatter`; both flow into `_sync_doc_thread`, which resolves via `resolve_messages(ours, fm)` (a sender-free thread yields the identical bare-string `Thread` as before). Slack **allows** a custom `op_sender` (unlike Discord — the OP is a normal `chat.postMessage`). Per-sender needs an `xoxb-` bot token (`core.sync`'s existing sender-override guard on `xoxp-`). The two `DocMessage` rebuilds the terraform machinery runs between read and post — ref-placeholder substitution (`refs._substitute_message`) and custom-emoji substitution (`_resolve_custom_emoji`) — now copy `sender` (they silently dropped it, which is why the naive wiring would have posted bare); `_resolve_and_edit_refs` (phase-3) also takes `frontmatter_by_slug` so a ref re-sync doesn't see a sender mismatch and cascade-repost.
- **`discord push`** (done):
  - No senders in the doc → unchanged (bot-only, as today).
  - Any reply carries a sender → **require `THRDS_DISCORD_WEBHOOK`** (new env, the URL is a secret — never a flag, never echoed), build `DiscordHybridClient(DiscordClient(...), DiscordWebhookClient(webhook))`, and `sync` through it. Missing webhook → raise naming it (this is the config-resolver deferred in `discord-push.md` #5).
  - **`op_sender` on Discord → error**: the OP anchors the thread as the bot's single identity (a webhook can't open a thread), so a custom OP sender is unsupported — matches `DiscordHybridClient`'s existing OP-override raise. (Slack allows it.)

## Round-trip

`parse_thread(serialize_thread(t, fm)) == (t, fm)` must still hold with senders: serialize emits the `sender.*` / `op_sender` frontmatter (canonical key order) and `+++ as <name>` for replies. `pull` populates sender only where `list_messages` does (Slack yes; Discord leaves sender fields `None` — open gap #2 in `discord-push.md`), so **doc → push is lossless; a Discord `pull` → doc won't reconstruct `as` refs until #2 lands.** Documented, not blocking.

## Tests (exact-equality / parsed-structure)

- `md`: parse + serialize round-trip with `sender.*` + `op_sender` + `+++ as`; `:emoji:` → `icon_emoji`, URL → `icon_url`; unknown ref → error; `as` + `@author` same line → error; a sender-free doc still yields bare `list[str]`.
- `resolve_messages`: OP + replies → exact `[Msg(...), …]`; foreign excluded.
- `slack push` / `promote`: doc with senders → `chat.postMessage` gets the right `username`/`icon_url`/`icon_emoji` per message (OP via `op_sender`, replies via `+++ as`); a sender survives a ref-carrying thread's `DocMessage` rebuild; `_resolve_custom_emoji` preserves `sender`; a sender-free push posts every message bare.
- `discord push`: senders + webhook env → routes through `DiscordHybridClient` (bot OP+thread, webhook per-sender replies — exact call split); senders + no webhook → raises naming the env; `op_sender` on Discord → raises.

## Out of scope

Sender syntax in the legacy multi-thread `parse_doc` layout; auto-reposting on sender drift (already deferred in `per-message-sender.md`); closing Discord's `list_messages` foreign/sender gap (#2).

## Implemented (2026-09-14)

Shared doc/md layer + both `discord push` and `slack push`/`promote` wired — cross-platform parity.

- **`doc.py`**: `SenderProfile` (name/icon_url/icon_emoji); `DocMessage.sender`; `Frontmatter.op_sender` + `senders: dict[str, SenderProfile]`.
- **`md.py`**: `+++ as <name>` in the reply grammar (a `+++ …` line that isn't a valid delimiter now raises rather than being swallowed as content, so `+++ @a as b` is a clear error); `sender.<name>.{name,avatar}` + `op_sender` frontmatter (flat keys, string-safe; `:emoji:` → `icon_emoji`, else `icon_url`); parse-time ref validation; canonical serialize (round-trip preserved); `op_sender` rejected in multi-thread `parse_doc`. New `resolve_messages(messages, frontmatter) -> list[str | Msg]` — the doc→`sync` bridge; a sender-free doc yields the identical `list[str]` as before.
- **`cli.py` `discord push`**: resolves messages; any reply with a sender routes through `DiscordHybridClient` and **requires `THRDS_DISCORD_WEBHOOK`** (secret, never a flag; a fresh dry-run needs neither token nor webhook); a custom `op_sender` on Discord raises (the OP anchors the thread as the bot). This is the config-resolver deferred in `discord-push.md` #5.
- **`slack push` / `promote` (parity)**: `threadfile.read_thread` / `read_threads` / `find_thread` now return `ParsedThread` (thread + frontmatter). `cli.py` builds `frontmatter_by_slug` from the parsed files and hands it to `sync_threads_staging`; `promote` passes the thread's `frontmatter`. `slack.py._sync_doc_thread` gained a `frontmatter` param and resolves via `resolve_messages` (the old bare-string build + the interim per-sender guard are gone); `sync_threads_staging`, `promote_thread`, and `_resolve_and_edit_refs` thread the frontmatter down. `refs._substitute_message` and `slack._resolve_custom_emoji` now copy `DocMessage.sender` through their rebuilds (they dropped it before). Slack allows a custom `op_sender`; per-sender needs an `xoxb-` token.
- Tests: `test_doc_sender.py` (11 — parse/serialize/round-trip/validation/`resolve_messages`); `test_discord_cli.py` +3 (webhook-routed per-sender push, missing-webhook raise, `op_sender` raise); `test_slack_sender_push.py` (6 — staging reply-as-sender, `op_sender`, sender-survives-ref-rebuild, sender-free-all-bare, `promote` reply-as-sender, `_resolve_custom_emoji` preserves `sender`). One spy stub (`test_adopt_and_reorder`) widened to accept `**kw`. Two existing OP-author error-message assertions updated (message widened to name `sender`).

Doc→push is lossless on both platforms; a Discord `pull`→doc won't reconstruct `as` refs until `discord-push.md` #2 (foreign/sender gap) lands (Slack `pull` already does, since `list_messages` carries author/sender).
