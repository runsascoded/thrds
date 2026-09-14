# Discord application emoji + app-owned webhooks (finding from the mgu digest twin)

Written from the mgu session on 2026-09-14 after staging `gcs-usage digest -P discord` (spec: `marin-gcs-usage/specs/done/discord-digest-twin.md`). Two small asks at the end; the finding matters for the README capability matrix.

## Finding: custom emoji in webhook messages depend on who owns the webhook

- Application emoji (`POST /applications/{app}/emojis`, the Slack-workspace-emoji analog thrds recommended) render as `<:name:id>` only when the **poster can use them**: the bot itself, or a webhook **whose `application_id` is that app** (one the bot created via `POST /channels/{id}/webhooks`).
- Through a user-created webhook (`application_id: null` — what a channel's "Integrations → Webhooks → New" produces, and what mgu's `#marin-bot-dbg` staging webhook is) Discord **silently rewrites** `<:arrow_degm30:123…>` to the bare text `:arrow_degm30:` on ingest. No error, no 4xx; the readback content is already stripped.
- Guild emoji work from any webhook in that guild, but need `CREATE_GUILD_EXPRESSIONS` and land in the server's picker.
- Creating an app-owned webhook needs `MANAGE_WEBHOOKS` on the channel (Marin Bot has none today, so the mgu twin is parked on a grant).
- Emoji names are `[A-Za-z0-9_]{2,32}` — no `-`, so mgu maps Slack's `arrow_deg-30` to `arrow_degm30`.
- Unrelated but bit us: bot REST calls from `urllib` get a Cloudflare 403 (`error code: 1010`) without a `DiscordBot (url, version)` User-Agent; curl's default UA is fine, which is why thrds never saw it.

Suggested matrix rows / footnote: "Custom emoji in text: Slack `:name:` (workspace) · Discord `<:name:id>` — app emoji only from the bot or an app-owned webhook; user-created webhooks strip them to `:name:`."

## Asks (small)

1. `DiscordClient.create_webhook(name, *, avatar=None) -> str` (returns the webhook URL; `GET /channels/{id}/webhooks` first to reuse an existing app-owned one by name). Lets a consumer bootstrap the hybrid pair from a bot token alone, and guarantees app-emoji access.
2. `DiscordClient.app_emojis() -> dict[str, str]` + `upload_app_emoji(name, png)`; optionally a doc-side `:name:` → `<:name:id>` rewrite in `discord push` using that map (Slack's `:name:` stays as-is). mgu carries these three calls in `gcs_usage/discord_api.py` today and would delete them.

Neither blocks mgu; both are pure additions.

## Implemented (2026-09-14): the three primitives

`DiscordClient` gains `application_id` (lazy `GET /applications/@me`, cached — the app, distinct from `bot_user_id`) and:

- **`create_webhook(name, *, avatar=None) -> str`** — `GET /channels/{id}/webhooks`, reuse an existing webhook that matches `name` **and** is app-owned (`application_id == self.application_id`), else `POST` a new one; returns `https://discord.com/api/webhooks/{id}/{token}`. A same-name *user-created* webhook (app-id null) is deliberately **not** reused (it can't post app emoji). Needs `MANAGE_WEBHOOKS`.
- **`app_emojis() -> dict[str, str]`** — `{name: id}` from `GET /applications/{app}/emojis` (handles the `{"items": […]}` envelope).
- **`upload_app_emoji(name, png) -> str`** — `POST /applications/{app}/emojis` with a `data:image/png;base64,…` body; validates `name` against `[A-Za-z0-9_]{2,32}` (raises on `-`) and returns the new id.

These three are what mgu deletes from `gcs_usage/discord_api.py`. Tests in `test_discord_app_emoji.py` assert exact request shapes (create/reuse/ignore-user-webhook, the emoji map, the data-URI upload, the name guard).

### Deferred: the doc-side `:name:` → `<:name:id>` rewrite

Not shipped yet, because of a round-trip footgun this spec itself documents: a **user-created** webhook strips `<:name:id>` back to `:name:` on ingest, so rewriting desired content to `<:name:id>` would mismatch the read-back and re-edit every push. The rewrite is only idempotent when every emoji-bearing message posts through the **bot or an app-owned webhook** — so it should land *after*/*with* the hybrid defaulting to `create_webhook` (app-owned), and gate the rewrite on app-ownership. mgu keeps its own rewrite until then (the "optional" half of ask #2).
