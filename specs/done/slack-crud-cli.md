# Spec: low-level Slack CRUD CLI (`thrds slack …`)

**Status:** implemented 2026-08-11. Motivated by the OA GCS-usage digest work, where basic one-off Slack operations — "list the last N messages with their ts", "delete this message by ts", "read this thread", "post one message as sender X" — got re-implemented **over and over as raw `urllib` `chat.*` / `conversations.*` heredocs** in throwaway scripts with hardcoded ts arrays. That's the `/ad-hoc-scripts` anti-pattern: recurring inline scripts that should be a CLI. thrds already has the primitives and an auth path; it just doesn't expose them as verbs.

## Divergences from this draft when implemented

- **`raw=` param on `SlackClient` used, not `_request` in the CLI.** The spec's "prefer a `raw=` param" recommendation is what shipped: `SlackClient.__init__(raw=False)` + `post/edit(raw: bool | None = None)` with message-override → client-default → False precedence (see `specs/done/raw-mrkdwn-passthrough.md`). The CLI passes `raw=not markdown` on every `post`/`edit` call.
- **`list_channel_history` + `list_thread_raw` added.** Two new `SlackClient` methods that take `channel` explicitly (like `scan_thrds_metadata`) and return raw Slack msg dicts (not typed `Message`s). Wire text is not roundtripped through `to_markdown` — the CRUD view is a low-level "what's on Slack" reading, symmetric with the raw-by-default `post`/`edit` write path.
- **`.thrds-rc` aliases shipped.** The spec called them "nice-to-have once the verbs settle" — since they're a 6-line addition and the verb shapes are stable, added them alongside: `tsh`/`tst`/`tsr`/`tsp`/`tse`/`tspl`.
- **Sender column: no `users.info` resolution.** The `history` / `thread` tables show `username` (custom sender) → `user` (id) → `bot_id` unresolved. Adding `users.info` cache lookups per unique user would add latency; `-j/--json` gives the full dict for callers that want the resolved name.

## Motivation (the recurring shapes)

Every one of these came up multiple times in a single session, each hand-written as a raw API call:

- **history** — `conversations.history?channel&limit` → print `ts · sender · text` (to find a message's ts, or eyeball channel state).
- **thread** — `conversations.replies?channel&ts` → print the OP + replies (verify a threaded post).
- **delete by ts** — `chat.delete` for one or more ts (tear down test posts / old-format posts).
- **post one-off** — `chat.postMessage` with an optional per-message `username` / `icon_url` (+ `thread_ts`).
- **edit** — `chat.update` a message's text.

All of it is `SlackClient` territory already: `list_messages` (`slack.py:194`), `delete` (`:328`, orphan-guarded), `permalink` (`:320`), `get_reactions` (`:230`), and `_request` for the raw calls. The session/doc CLI (`init`/`push`/`pull`/`diff`/`archive`/`list-sessions`) never surfaces them.

## Current state

- `thrds/cli.py` is a `click.group()` of high-level session verbs only.
- `_make_slack_client()` (`cli.py:63`) already builds a `SlackClient` from env (`THRDS_SLACK_TOKEN` / channel resolution incl. `#name` → id). Reuse it verbatim.
- `SlackClient` exposes `list_messages`, `delete(orphans_ok=)`, `permalink`, `post`, `edit`, `_request`.

## Proposed: a `slack` subgroup

Add `@cli.group('slack')` with these verbs. All reuse `_make_slack_client()`; channel comes from the arg (accepting `#name` or id via the existing resolver) or `THRDS_SLACK_CHANNEL`.

```
thrds slack history <channel> [-n/--limit 20] [-j/--json]
thrds slack thread  <channel> <ts>        [-j/--json]
thrds slack rm      <channel> <ts>...     [-f/--force]     # force → orphans_ok
thrds slack post    <channel> [-u/--username U] [-i/--icon-url URL] [-e/--icon-emoji E] [-t/--thread-ts TS] [-m/--markdown] <text>
thrds slack edit    <channel> <ts> [-m/--markdown] <text>
thrds slack permalink <channel> <ts>
```

- **history / thread** — default to a compact human table (`ts  sender  text-first-line`); `--json` emits the raw message dicts for scripting (the `jq`-able form the heredocs were reaching for). `thread` wraps `list_messages`; `history` wraps a `conversations.history` `_request`.
- **rm** — variadic ts; deletes each via `SlackClient.delete` (so the orphan guard applies). `--force` passes `orphans_ok=True`. Print per-ts ok/error.
- **post / edit** — thin wrappers over `SlackClient.post` / `.edit`, exposing the per-message sender fields from `specs/per-message-sender.md` (`--username`/`--icon-url`/`--icon-emoji`). `post` takes `--thread-ts` to reply.

## Key design decision: raw by default, `--markdown` to convert

The high-level flow always runs `to_slack()` (local markdown → Slack mrkdwn). For **ad-hoc CRUD that's the wrong default** — the whole GCS-usage session bled from `to_slack` mangling text that was *already* Slack mrkdwn (single-`*` bold read as italic → `_..._`). A CRUD `post`/`edit` should send **text verbatim** (raw Slack mrkdwn) by default, with `-m/--markdown` to opt into conversion. This is the one deliberate divergence from the session verbs; call it out in `--help`.

(Implementation: `post`/`edit` here should bypass `SlackClient.post`/`.edit`'s built-in `to_slack` — either add a `raw: bool` param to those methods, or `_request` directly in the CLI. Prefer a `raw=` param so both paths share payload/scope handling.)

## Platform scope

Slack only — this is a `slack` subgroup wrapping `SlackClient`. Discord/Bluesky have their own clients; a parallel `discord`/`bsky` CRUD group is a later, separate spec if wanted. Don't try to abstract across platforms now.

## Testing

- `history`/`thread` table + `--json` shape (mock `_request` → assert exact rendered lines / exact parsed dicts; follow the repo's exact-equality assertion style).
- `rm` calls `delete` per ts; `--force` → `orphans_ok=True`; orphan error surfaced without `--force`.
- `post` raw vs `--markdown`: raw sends `text` byte-identical; `--markdown` sends `to_slack(text)`. Sender fields land in the payload (ties to `per-message-sender.md`).
- Channel resolution: `#name` and bare id both accepted.

## Out of scope

- Discord/Bluesky CRUD groups.
- Reaction add/remove verbs (a `get_reactions` read exists; add/remove can be a follow-up if they recur).
- A `.thrds-rc` alias set (`sh`, `sr`, …) — nice-to-have once the verbs settle.
