# Transport robustness: Discord `_curl` error handling, Slack `msg_too_long` chunking

Two production failures in marin-discord's Weekly Summary GHA ([run 29819813824] on 2026-07-21, [run 28436869218] on 2026-06-30) trace to thrds transport-layer fragility. Each failed run left a permanent gap in the published summaries (weeks of 2026-07-13 and 2026-06-22 respectively — the workflow doesn't backfill). This spec covers hardening both paths.

## Failure A: Discord `_curl` crashes on non-JSON response

### What happened
2026-07-21, `DiscordClient.post` → `_curl("POST", f"/channels/{channel}/messages", data)` → `json.loads(result.stdout)` raised `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` (`thrds/discord.py:42`).

`_curl` already returns `None` for empty/whitespace stdout, so stdout was non-empty non-JSON — almost certainly a Cloudflare/Discord HTML or plain-text error page (5xx interstitial, or CF's `error code: 1015` rate-limit text). Transient — the next week's run succeeded with no code change.

### Current weaknesses in `_curl` (`thrds/discord.py:24-47`)
- `curl -s` discards the HTTP status; there's no way to distinguish 200 from 502 from 429.
- Non-JSON bodies crash with a bare `JSONDecodeError` carrying no diagnostic context (no status, no body snippet).
- `check=True` means curl transport failures (DNS, TLS, connection reset) raise `CalledProcessError`, also context-free (stderr is captured but not surfaced).
- No retry of any kind. Discord 429 JSON bodies (`retry_after`) are not honored; 5xx are not retried.
- The empty-stdout `return None` path is itself a trap: callers like `post()` immediately do `resp["id"]` → opaque `TypeError`. Empty body on a POST is an error, not a valid state.

### Requirements
1. **Keep curl** as the transport (FFR: urllib was tried and abandoned for Discord; curl is deliberate).
2. **Capture the HTTP status**, e.g. `-w '\n%{http_code}'` and split it off the body (or `-o <tmpfile>` + `-w '%{http_code}'`).
3. **Retry with EB + jitter** on: curl nonzero exit (transport), HTTP 5xx, HTTP 429 (honor `retry_after` from the JSON body when parseable, else `Retry-After` semantics / EB fallback), and non-JSON body with 2xx status (CF interstitials sometimes 200). Bounded attempts (e.g. 4), total wait capped around a minute — callers are batch jobs, not interactive.
4. **On persistent failure, raise `RuntimeError`** including: method, path, HTTP status, first ~500 chars of body, and curl stderr if nonzero exit. Never let a bare `JSONDecodeError`/`CalledProcessError` escape.
5. **Distinguish "empty body" by method/status**: 204-No-Content (e.g. DELETE) is valid → `None`; empty body on a 2xx POST/GET that should return an entity is an error → retry/raise. Callers that can legitimately receive `None` should be the only ones handling it.
6. Existing structured-error handling (Discord `{"code": ..., "message": ...}` dicts, `EditRateLimited` for code 30046) stays as-is, applied after JSON parse succeeds.

## Failure B: Slack `msg_too_long` in `sync_linked` phase-4 edit

### What happened
2026-06-30, `SlackClient.sync_linked` phase 4 (rebuild summaries with real permalinks) → `edit` → `chat.update` → `RuntimeError: Slack API error: msg_too_long` (`thrds/slack.py:313` → `:146` → `:82`).

### Root causes in `build_summary_messages` (`thrds/linked.py:89`) and `sync_linked` (`thrds/slack.py`)
1. **Oversized single bullet is never split**: the greedy packer, when one bullet alone exceeds `limit`, sets `current = bullet` and emits it as its own message — over-limit, guaranteed `msg_too_long` at post/edit time.
2. **Placeholder→real-link growth can change the message count**: phase 1 packs with placeholder URLs, phase 4 repacks with real permalinks (long — often 100+ chars each). If the repack overflows into more messages than were originally posted, `zip(summary_ids, final_summaries)` **silently drops the extras** — latent content-loss bug even when nothing raises.
3. No length guard at the `post`/`edit` boundary (Discord's client checks `MESSAGE_LIMIT`; Slack's doesn't).

### Requirements
1. **Hard-split oversized bullets** in the packer: a bullet > `limit` is split at a line/word boundary (ellipsis continuation), so every emitted message is ≤ `limit` by construction. Applies to `build_detail_messages` too if it shares the pattern.
2. **Stabilize packing across phases**: pad placeholder URLs to the worst-case real-permalink length (or repack-then-reconcile). Phase-4 repack must produce the same message count as phase 1; if reconciliation is chosen instead of padding, extra messages get posted (and shortfall messages get edited to a benign placeholder or deleted-if-safe — but per the no-delete-thread-parents rule, never the parent).
3. **Count mismatch must raise**, never silent `zip` truncation. `strict=True` at minimum, better a reconcile step per (2).
4. **Length assert at the `post`/`edit` boundary** in `SlackClient`, mirroring Discord's `MESSAGE_LIMIT` check, so future packing bugs fail fast with a clear message instead of a Slack API round-trip error.

## Testing
Per root testing conventions (exact-equality assertions, no substring `in` checks):
- Packer: feed a bullet > limit; assert the exact list of emitted messages (split points, ellipses), every message `len ≤ limit`.
- Packer stability: placeholder vs real-link URL lengths → assert equal message counts.
- `_curl` retry: fake curl via a stub script or monkeypatched `subprocess.run` returning, in sequence: HTML body + 502, then valid JSON + 200; assert the exact parsed result and the number of attempts. Assert raised `RuntimeError` message shape (normalized) for the persistent-failure case.
- TFFP: each fix lands with a test that reproduced the failure first.

## Rollout
- marin-discord's weekly-summary workflow installs thrds at whatever ref it pins — bump after this lands.
- Separately (marin-discord side, not thrds): the workflow should commit generated summary files before posting, and backfill missing weeks; tracked over there.

[run 29819813824]: https://github.com/Open-Athena/marin-discord/actions/runs/29819813824
[run 28436869218]: https://github.com/Open-Athena/marin-discord/actions/runs/28436869218
