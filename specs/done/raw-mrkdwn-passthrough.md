# Raw-mrkdwn passthrough on `SlackClient.post` / `edit`

**Status:** implemented 2026-08-11. Baseline for `specs/slack-crud-cli.md`
(the CRUD CLI's `post`/`edit` verbs default to raw and expose `-m/--markdown`
to opt into conversion).

## Motivation

watchy (`~/c/rac/watchy`) iterates on Slack message formats in a staging channel: its CFW renderer emits **raw Slack mrkdwn** (`<url|text>` links, `:emoji:` shortcodes, `*bold*`, `_italic_`), and demo scripts replay that output via `chat.postMessage`. `SlackClient.post()` would be a natural fit (it already supports per-message `username` / `icon_url` / `icon_emoji`, pacing, metadata), but it unconditionally runs content through `to_slack()` md→mrkdwn conversion, which mangles pre-rendered mrkdwn — e.g. `*173 followers*` → `_173 followers_` (single `*` is em in md, bold in mrkdwn).

So watchy's demo scripts hand-roll `urllib` calls instead of reusing thrds. Same applies to `delete`-then-repost iteration loops: list/delete work fine via thrds today; only posting is blocked.

## Ask

- `post(content, ..., raw: bool | None = None)` and `edit(message_id, content, *, raw: bool | None = None)`: when `raw` resolves to `True`, skip `to_slack()` and send `content` as the wire `text` verbatim.
- Client-wide default via `SlackClient(..., raw: bool = False)`; per-call resolution is **message override → client default → False** — parallel to the `username` / `icon_url` / `icon_emoji` precedent in `specs/done/per-message-sender.md`. Explicitly:
  - `SlackClient()` + `post(x)` → converted (both defaults false)
  - `SlackClient(raw=True)` + `post(x)` → raw (client default)
  - `SlackClient()` + `post(x, raw=True)` → raw (per-call)
  - `SlackClient(raw=True)` + `post(x, raw=False)` → converted (per-call wins)
- Char-limit check still applies; metadata/sender/unfurl handling unchanged.

## Non-goals

- No reverse conversion (`list_messages` already returns wire text for non-thrds messages).
- No md sniffing/auto-detection — explicit flag only.
