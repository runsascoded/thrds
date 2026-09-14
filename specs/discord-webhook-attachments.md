# Spec: `DiscordWebhookClient` — file attachments + `allowed_mentions`

Two small additions to the webhook transport, both needed by the first production consumer of it: `gcs-usage weekly` in `marin-gcs-usage` (`specs/weekly-discord-report.md` there), which replaces Marin's weekly `#internal-discuss` storage report with one webhook message posted as **GCS usage** — text plus a treemap PNG of the week's changes. Nothing here touches the bot client, `sync`, or the CLI.

## 1. Attachments — `post(..., files=[...])`

A webhook message carries files as **multipart**: `payload_json` (the same JSON body `post` builds today) plus one `files[n]` part per file. Today `_curl_raw` sends only `-d <json>`, so there is no way to attach anything.

- `DiscordWebhookClient.post(content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None, files: Sequence[Path] = ())`. Empty `files` → byte-identical request to today (JSON body, `Content-Type: application/json`). Non-empty → multipart: `-F payload_json=<json>;type=application/json` and `-F files[i]=@<path>` per file (curl infers the part's content type from the extension; PNG is the case that matters). `payload_json` additionally lists `attachments: [{"id": i, "filename": name}]` so Discord keeps the order and the original filenames.
- `edit(message_id, content, *, files=(), keep_attachments=True)`: `PATCH` with the same multipart shape adds files; an `attachments: []` in `payload_json` drops the existing ones. Default keeps them (an edit that only changes text must not strip the image).
- `_curl_raw` grows a `form: list[tuple[str, str]] | None` alternative to `data` — each tuple becomes one `-F` — so the retry/backoff/429/label handling stays in one place. `data` and `form` are mutually exclusive (assert). Headers: the caller omits `Content-Type` for multipart (curl sets the boundary); the JSON path is unchanged.
- Limits, checked client-side with a clear `ValueError`: 10 files per message; 8 MiB per file at the default upload tier (Discord returns `40005 Request entity too large` past the server's limit — pass that through as today's HTTP error, no special casing).
- The image renders inline under the text like any user-uploaded attachment; `suppress_embeds` (`flags=4`) does **not** hide attachments, only link previews — worth one live assertion, since the consumer sets both.

**Tests** (exact request shapes, like the existing `test_discord_webhook_client` suite): a stubbed `_curl_raw` asserting `form == [("payload_json", '{"content": …, "attachments": [{"id": 0, "filename": "diff.png"}]}'), ("files[0]", "@/abs/diff.png")]` and `data is None`; the empty-`files` call unchanged; the 11-file `ValueError`. One live probe against `#bot-test` (`tmp/…`) posting a PNG + text, then editing the text and reading the attachment back on the message via the bot's `_list_raw` (the `attachments` array).

## 2. `allowed_mentions`

`content` is posted raw, so a message quoting a path or a name that happens to read `@here`, `@everyone`, or `@role` pings people. The weekly report interpolates bucket prefixes and owner short names, so it needs mentions off; that is the safe default for any bot-authored report.

- Constructor kwarg `allowed_mentions: dict | None = None` on `DiscordWebhookClient` (and, for symmetry, `DiscordClient` — same JSON field on `POST /channels/{id}/messages`). When set, every `post`/`edit` body carries it verbatim. `None` → field absent → Discord's default (parse everything), i.e. today's behaviour.
- The consumer passes `{"parse": []}`; thrds ships that as `NO_MENTIONS` in `thrds.discord` so callers don't hand-write the dict.
- `DiscordHybridClient` forwards nothing new: it constructs neither client, so each side's kwarg applies to the messages it authors.

**Tests**: `post` and `edit` bodies carry `"allowed_mentions": {"parse": []}` when set and lack the key when not; both clients.

## 3. A custom sender on the OP (`op_sender` on Discord)

Today `DiscordHybridClient.post` raises on an OP with a sender override and `discord push` rejects `op_sender` ("the OP anchors the thread as the bot's single identity, so a webhook can't open a thread"). That conflates two things. A webhook can't *open* a thread, but the bot can open one off **any** message in the channel — `POST /channels/{channel_id}/messages/{message_id}/threads` takes a message id, and a webhook-authored message is a message in the channel like any other (`MANAGE_THREADS`/`CREATE_PUBLIC_THREADS` on the bot, no ownership requirement). So the hybrid could post the OP through the webhook when it carries a sender, then `create_thread` off it with the bot; OP edits route to the webhook like any other webhook-authored message (`_webhook_ids` already tracks that), and a lone OP with a sender needs no thread at all.

- **Probe first** (`tmp/…` against `#bot-test`): webhook-post an OP with `username`/`avatar_url`; bot `create_thread(op_id)`; bot posts a reply into it; webhook `PATCH`es the OP. Record the four statuses in this spec. If Discord refuses the thread on a webhook message, this item dies here and the current raise stays, with the finding written down.
- **If it works**: `DiscordHybridClient.post(thread_id=None, username=…)` → webhook; `open_thread` unchanged (bot, off whatever id the OP got); the re-push discriminator is unchanged (`op_id == thread_id` still holds — the thread takes the OP's snowflake regardless of who authored it — worth asserting in the probe). `discord push` drops the `op_sender` rejection. `sync_linked` phase-4 edits the OP through `_channel_for` → webhook when `op_id ∈ _webhook_ids`.
- Consumer: a hand-authored weekly post (`dscrd/weekly/` session, `op_sender: gcs`) becomes pushable to `#marin-bot-dbg` with `thrds discord push` for draft iteration, in addition to the job's direct library call.

## Consumer

`gcs-usage weekly` (marin-gcs-usage) will call:

```python
hook = DiscordWebhookClient(url, username="GCS usage", avatar_url=ICON, suppress_embeds=True, allowed_mentions=NO_MENTIONS)
hook.post(content, files=[png])
```

Its `thrds` pin (`gcs-usage/pyproject.toml`) moves to the pushed `py` head once this lands; until then it posts text-only.

[weekly-discord-report]: ../../oa/marin-gcs-usage/specs/weekly-discord-report.md
