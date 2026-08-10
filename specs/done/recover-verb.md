# Spec: `thrds recover` verb

**Status:** done (2026-08-10, commits `bde5a3f` + `583aee1`). Closes the last outstanding item from `multi-thread-posts-and-capture.md` — the durability story the metadata trail was designed for.

## Implementation summary

Landed:
- **`SlackClient.scan_thrds_metadata(channel, *, oldest, max_pages, on_page)`** in `slack.py`: paginates `conversations.history` with `include_all_metadata=True`, filters `event_type='thrds'`, groups by `session_id`, returns `{sid: RecoveredSession}`. Raises `ValueError` on within-session `doc_slug` disagreement (corruption).
- **`RecoveredSession`** dataclass — the summary tuple exported at package level (via `thrds.RecoveredSession`) so downstream tools can consume the scan output directly.
- **`ScanCapReached`** exception (extends `RuntimeError`) — raised by scan when `max_pages` exhausts before `has_more` clears; distinct type so callers can catch without swallowing real failures.
- **`thrds recover CHANNEL`** CLI verb with flags `-i/--session-id`, `-s/--staging`, `-W/--no-write-doc`, `-d/--oldest-days N`, `-m/--max-pages N` (default 50).
- **12 scan-side unit tests** (`tests/test_recover.py`) + **14 CLI tests** (`tests/test_cli.py`). Full suite: 285 passed, 2 skipped, 0 failures.
- **Live-verified**: recovered trainium session (`C0BNK26CASV`) byte-identically — both `thrds.json` and `trainium-2026-07-31.md` (including custom emoji).

Divergences from the design body below (preserved as historical record):

- **Scan caps beyond the design.** The original spec's "algorithm" section paginated unboundedly. Added `--oldest-days` / `--max-pages` / `on_page` progress log because Slack does not index metadata (there is no `has_metadata:` search operator), so scan cost on busy prod channels is a real concern. Default cap: 50 pages (~10k messages); user narrows with `--oldest-days` when they know a rough post date.
- **`--session-id` is a flag, not a filter.** Bare `recover CHANNEL` on a single-session channel auto-selects; on a multi-session channel prints a table and exits 2 (per the spec). Explicit `-i SID` overrides both. No separate `list-sessions` subcommand yet (see follow-ups).

## Why

Every posted/edited message carries Slack `metadata`:
```
event_type = "thrds"
event_payload = {session_id, doc_slug, thread_slug, kind}
```
(`slack.py::_thrds_metadata`, stamped from both `_sync_doc_thread` and `_sync_preamble`.)

That metadata is the durable source of truth; `thrds.json` is a local write-through cache. Today, losing the session dir strands the threads — the slug→ts map is gone and there's no way to keep editing/pulling the doc without hand-reconstructing pointers.

`recover` walks Slack for `event_type=thrds` messages in a channel, rebuilds `thrds.json` from them, and (optionally) writes back `<doc_slug>.md` via the existing `pull_doc_*` path.

## Scope (v1)

Handle the primary case: **given a channel + session_id, rebuild state**. Optional session_id discovery (list sessions in a channel, pick one) is a small usability nicety on top of the same scan.

Non-goals:
- Recovering ownership across workspaces (a `xoxp-` token only sees what it sees).
- Fabricating metadata for hand-posted threads that never went through `thrds` (no metadata → not recoverable).
- Undoing archive (staging PC can be unarchived out-of-band via `conversations.unarchive`; not `recover`'s job).

## CLI shape

```
thrds recover <channel> [--session-id ID] [--staging | --prod] [--write-doc / --no-write-doc]
```

- `<channel>` — Slack channel ID (`C…`) to scan. Required. (Channel *name* resolution is a general CLI ergonomics gap, orthogonal.)
- `--session-id` — if omitted and >1 session found in channel, list them and exit (`thrds recover <channel>` alone acts as "list sessions here"). If exactly one session in channel, use it.
- `--staging` / `--prod` — routes the recovered pointers to `staging_threads` vs `prod_threads[channel]`. Default: `--prod` (staging channels are ephemeral by design; prod is where recover matters). Mutually exclusive.
- `--write-doc` — after rebuilding state, also `pull_doc_*` to regenerate `<doc_slug>.md`. Default on; `--no-write-doc` skips (state-only recovery).

Preconditions: run inside a **prepared session dir** — either a fresh dir the user has `mkdir`'d, or a directory that already has partial state. `recover` writes `thrds.json` and (with `--write-doc`) `<doc_slug>.md`. It does **not** call `thrds init` (no gist creation, no git init) — that's the user's call afterward if they want mirroring.

Rationale: `recover` is a data-plane op (Slack → local files). Coupling it to `init` would either require a working gist (which recovery scenarios may not have) or duplicate init's env checks.

## Algorithm

1. **Scan channel**:
   ```
   conversations.history(channel=<channel>, include_all_metadata=True, limit=200)
   ```
   Paginate via `response_metadata.next_cursor` until exhausted. For each top-level message, check `metadata.event_type == 'thrds'`. Retain matches; index by `event_payload.session_id`.

2. **Session-id resolution**:
   - If `--session-id` given: filter to that id. Raise if 0 matches.
   - Else if exactly one distinct session_id present: use it.
   - Else: print a table (`session_id | doc_slug | thread_count | oldest_ts | newest_ts`) and exit code 2.

3. **Rebuild slug → ts map**:
   - For each retained top-level message with `event_payload.kind in ('op', 'preamble')`:
     - `preamble` → `preamble_ts = msg.ts` (there should be at most one).
     - `op` → `threads[event_payload.thread_slug] = msg.ts`.
   - Ignore `kind == 'reply'` at this level (replies live under an OP; the OP's ts is what matters for slug pointers). Replies-with-metadata still exist in the scan; they just don't contribute to the map.

4. **Extract session-wide fields from metadata** (should agree across all matches; verify + raise on inconsistency):
   - `session_id`
   - `doc_slug`
   - Note: `channel_prefix` is not in the metadata; recover leaves `channel_prefix=None` and lets the env var/user-override take over on next push.

5. **Assemble `SessionState`**:
   ```python
   if --staging:
       state.staging_channel = channel
       state.staging_preamble_ts = preamble_ts
       state.staging_threads = threads
   else:  # --prod
       state.prod_channel = channel
       state.prod_preamble_ts[channel] = preamble_ts   # skip if None
       state.prod_threads[channel] = threads
   state.doc_path = f"{doc_slug}.md"
   ```
   - `session_id` from metadata (**not** freshly minted — the whole point).
   - `gist_id` / `gist_remote` / `staging_archived` / `workspace_emoji` left as defaults (they're either irrelevant or discovered separately).

6. **Write `thrds.json`**.

7. **If `--write-doc`**: call the same `pull_doc_prod` / `pull_doc_staging` path used by `thrds pull --write`, passing `session_dir=Path.cwd()` for emoji resolution. This gives us the doc content plus emoji downloads for free.

## Slack API subtleties

- `include_all_metadata=True` on `conversations.history` — without it, `metadata` is not returned. (Verify: docs say `include_all_metadata` for `conversations.history` and `.replies` returns event metadata; the token needs the scope `metadata.message:read` — currently unclaimed by our app config, so this is a new scope requirement worth flagging in the docs.)
- Pagination: `limit=200` (Slack's max for history); loop on `has_more` / `next_cursor`.
- Rate limits: `conversations.history` is Tier 3 (~50 req/min); paginated recovery on a busy channel could bump into it. Add basic 429-respecting retry (already have EB machinery in `_request`; check it covers this).
- **Metadata visibility**: metadata is only returned to the app that posted it (per Slack docs). So `recover` only works with the same token/app that did the original posts. Cross-token recovery is out of scope (would need a workspace-admin API).

## Test plan

Unit tests, mirroring `test_cli.py`'s spy pattern for `SlackSpy`:

1. **Round-trip**: `sync_doc_prod` a small doc → capture posted metadata → `recover` from that captured state → assert `SessionState` equals the pre-recovery state (modulo fields intentionally reset).
2. **Multi-session channel**: two `session_id`s in the same channel scan → no `--session-id` → exits 2 with a list.
3. **`--session-id` filter**: same as (2) but with explicit `--session-id` → single-session result.
4. **`--write-doc`**: end-to-end recovery + pull → assert `<doc_slug>.md` matches the original.
5. **Metadata-shape inconsistency guard**: two messages with the same session_id but different `doc_slug` → raise.
6. **`--staging` vs `--prod` routing**: recovered pointers land in the correct field.
7. **Empty channel** / **no thrds metadata**: clear error message, no partial state file written.

## Follow-ups (deferred)

- **Channel-name resolution** (`<channel>` accepts `#name` too) — general CLI concern, not recover-specific.
- **`thrds list-sessions <channel>`** as a proper subcommand rather than "bare `recover` implicitly lists". Cleaner UX, small refactor.
- **Bidirectional metadata `metadata.message:read` scope** — needs to be added to the required-scopes list in the README, alongside `chat:write`, `emoji:read`, etc.
