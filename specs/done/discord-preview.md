# `thrds discord preview`: local Discord-faithful render + edit-write-back

Companion to `~/c/oa/discord-agent/specs/discord-md-parser.md` (the parser, which lives there — read that first). This spec covers the thrds side: the preview server, the vendored bundle, the shared fixture corpus, and eventually edit-write-back.

## Why (decision trail, 2026-08-26)

The Discord staging problem: prod delivery is copy-paste (self-bots violate ToS — and no, owning the server doesn't help; enforcement attaches to the *account* making user-token API calls, not the guild). The bot-in-a-private-server staging push (deferred phase 2b of `specs/done/discord-platform.md`) confirms render but forfeits UI editing: Discord only lets a message's author edit it, so bot-posted staging messages are read-only to the user, and user-posted messages are read-only to the bot. There is no arrangement of tokens that gets both.

A **local previewer** dissolves the constraint instead of working around it: render the draft `.md` Discord-faithfully in a local page, allow inline edits, write them back to the `.md` — which is the actual source of truth, so the round-trip that Discord's authorship model forecloses is trivial here. Each save can commit, so every iteration lands in the gist trajectory automatically ("snapshot versions as I iterate", without a `pull`).

Why not a generic markdown preview: Discord's renderer diverges from CommonMark exactly where it hurts — tables and `---` don't render, headings stop at `###`, `-#` is subtext, `__` is underline (not bold), masked links get interstitials, mentions/emoji/timestamps are `<…>` forms. A generic preview cheerfully renders the table Discord silently drops. Fidelity to *Discord's* renderer is the entire value.

The renderer is TS/React and lives in discord-agent (its `Message.tsx` is the most-constrained consumer — interactive mention components — and it's the second live regex-chain renderer this replaces). thrds consumes it.

## The vendoring constraint

thrds must stay `pip install`-able with no node toolchain. So the preview UI ships as a **prebuilt bundle vendored as package data** (`thrds/preview/dist/`), built in discord-agent (or the eventual extracted `$js` package) and copied in. Concretely:

- discord-agent's parser module gets a second Vite build target: a small standalone preview app (editor pane + rendered pane; no channel list, no archive coupling).
- A `sync-preview-bundle` script here copies `dist/` → `thrds/preview/dist/` and records the source repo + SHA in `thrds/preview/BUNDLE_PROVENANCE` (plain text: repo path, commit, build date). Regenerating is that script, not a by-hand copy.
- The bundle is committed (it's small, self-contained, versioned like any other vendored asset). Local dev against a live parser checkout uses the discord-agent dev server directly; the vendored bundle is for installed use.

## Phases

### 1. `thrds discord preview` (read-only render)

- New verb on the `discord` subgroup: starts a local HTTP server (stdlib `http.server` or similar — no new heavy deps), serves the vendored bundle, exposes the session's doc at `GET /doc` (raw md) — the page parses + renders client-side.
- Port: per-project convention — pick a stable default unlikely to collide (hash "thrds" into an eligible range; document it), `-p/--port` to override. Check-before-bind; error mentioning `kill-port` on conflict.
- `-O/--no-open` to suppress the auto browser-open, matching existing verbs.
- Watches the doc file (mtime poll is fine) and pushes reloads (SSE or poll from the page), so editor-side saves re-render live.
- `_load_state(expected_platform='discord')` guard like every other discord verb.
- Works with zero network and zero tokens — this is the whole point.

### 2. Warnings panel = the linter, aligned via the corpus

The preview page shows the render *and* the degradations ("this table renders as literal pipes", "`####` stays literal") — the parser knows both, since rendering-as-Discord-does *is* detecting the degradation.

`DiscordLinter` (Python, `thrds/lint.py`) stays: it's the cheap pre-push heuristic for terminal flows (`render`/`lint` verbs), and deriving it from a TS renderer across the language boundary isn't realistic. Alignment instead comes from the **shared fixture corpus**:

- Canonical corpus: `fixtures/discord-md/corpus.json` in discord-agent (see that spec for the case shape — `input` / `ast` / `warnings` / `verified`).
- thrds vendors a snapshot at `tests/fixtures/discord-md-corpus.json` + a sync script; a test asserts `DiscordLinter`'s output on each case's `input` matches the case's `warnings` (for the rule families the Python linter implements — it may cover a subset; the test declares which, explicitly, so gaps are visible rather than silent).
- This is the CommonMark-`spec.json` model: the corpus is canonical, neither implementation is. It's also where drift goes to die — the 2023 masked-link change produced a wrong lint rule here precisely because the lint rules were freehand rather than fixture-pinned (`specs/done/discord-masked-links-render.md`); today (2026-08-26) the table rule flagged every `---` horizontal rule for the same reason (fixed, `77c29bb`).

### 3. Edit-write-back

- The preview page gains an editable source pane (or contenteditable on the render — decide in implementation; source pane is simpler and lossless).
- `POST /doc` writes the full doc back to the session's `.md`. Full-file replace, no patch protocol — the doc is small and the file is the unit of versioning.
- On write: optionally auto-commit to the session repo (`-c/--commit` flag on `preview`, or a checkbox in the UI) so each save is a gist-trajectory version. Default off; explicit is better for the first cut.
- Concurrent-edit safety: the server holds the doc's mtime from its last read; a `POST` whose base mtime is stale (file changed on disk since the page loaded it) is rejected with the current content, page shows a merge prompt. Same shape as the push gate's lesson: compare against what you're *based on*, not what you last saw.

### 4 (only if still wanted): bot staging push

The deferred `discord-platform.md` phase 2b — `DiscordClient.sync()` into a private server via `THRDS_DISCORD_TOKEN` — stays deferred. Revisit only if the calibrated previewer proves insufficient (e.g. embeds/link-preview behavior, which a local renderer can't reproduce faithfully). The previewer likely obsoletes it; don't build both speculatively.

## Implemented 2026-08-26: phases 1+3 (server + write-back), stub UI

Landed as `thrds/preview/` + the `discord preview` verb (`08c1a03`) while discord-agent builds the parser — these phases don't depend on it, and the bundle drops into `thrds/preview/dist/` when ready. Deltas from the plan above:

- **No SSE** — the page polls `/doc` at 1s and reloads when clean, flags when dirty. Plenty for a localhost file.
- **Conflict check uses an opaque string token**, not a numeric mtime: `st_mtime_ns` exceeds JS `Number.MAX_SAFE_INTEGER`, so a numeric token is rounded by the page's `JSON.parse` and every save 409s. Found by driving the real page (the Python suite can't see it — Python json keeps ints exact); the page-side conflict UI is an inline banner with explicit reload/overwrite buttons, deliberately not `confirm()` (modal dialogs block browser automation).
- **Port 3077** (= 3000 + crc32("thrds") % 1000), `-p 0` for ephemeral.
- **`-c/--commit`** commits the doc per page-save; default off, per the plan.
- The vendored `dist/` needs two ignore-system carve-outs, now in place: `thrds/preview/.gitignore` un-ignores it past the global `dist` rule, and `[tool.hatch.build.targets.wheel] artifacts` keeps it in the wheel (verified in the built wheel).

Still open here: phase 2 (real renderer bundle + corpus-alignment test) once discord-agent's parser ships a preview build, and the `sync-preview-bundle` provenance script.

## The bundle is ready (discord-agent, 2026-08-28)

discord-agent shipped the preview build target (`8d2a8e9`), so phase 2's blocker is gone. See its `specs/discord-md-parser.md` → "The preview build target" for the full account.

To vendor it:

```bash
cd ~/c/oa/discord-agent/app && pnpm install && pnpm preview:build   # -> app/preview/dist/
```

`app/preview/dist/` is named to mirror `thrds/preview/dist/`; copy its contents in wholesale (`index.html` + `assets/`, 336 KB, entry 241 KB / 73 KB gzipped). No rewriting: assets are emitted with relative URLs (`base: './'`) precisely because this server is a stdlib static one that can't rewrite paths.

**Already verified against this repo's `server.py`**, not merely built — index, CSS and all 16 highlight.js chunks serve with zero 4xx, and the lint panel renders whatever `/lint` returns. That test loaded `thrds/preview/server.py` *by file path*, bypassing `thrds/__init__.py` (which imports the whole package, `emoji` included); worth knowing that the module's "no non-stdlib dependencies" claim holds for the module but not via a normal package import.

The page implements this repo's contract as specified, including the two decisions recorded above: `mtime` treated as an opaque string, and an inline conflict banner rather than `confirm()`. discord-agent's dev server (`pnpm preview:dev`, port 5274) reimplements `/doc` + `/lint` with the same semantics, 409 included, so the bundle can be iterated on there without a thrds checkout.

Interaction paths driven in a real browser there: spoiler reveal; edit → dirty → save → disk write; external edit while clean → reload; external edit while dirty → no clobber, then 409 → banner → overwrite.

Still open on this side, unchanged: `sync-preview-bundle` + `BUNDLE_PROVENANCE`, and the corpus-alignment test against `fixtures/discord-md/corpus.json` (56 cases).

## Completed 2026-08-28: bundle vendored, corpus aligned — acceptance met

- **`scripts/sync-preview-bundle`** builds the bundle in a discord-agent checkout (`pnpm preview:build`), replaces `thrds/preview/dist/` wholesale, snapshots the corpus to `tests/fixtures/discord-md-corpus.json`, and writes `thrds/preview/BUNDLE_PROVENANCE` (source, commit + dirty flag, build date). Vendored at discord-agent `8d2a8e9`; the full bundle (17 asset files) verified present in the built wheel.
- **Corpus-alignment test** (`tests/test_discord_corpus.py`): 51 cases (the "56" above was discord-agent's vitest count). `IMPLEMENTED = {discord/table, discord/raw-mention}`; `discord/heading-depth` and `discord/thematic-break` are declared renderer-side-only — a doc-level thematic-break lint would warn on thrds' own `---` separators (the 2026-08-26 table-rule bug, reintroduced on purpose).
- **The corpus caught a real linter bug on first contact**: `@everyone`/`@here` are the two name-form mentions Discord *does* resolve on paste, and `DiscordLinter`'s raw-mention rule flagged them. Fixed (regex exclusion), pinned in both `test_lint.py` and the corpus cases `everyone`/`here` — the fixture-corpus model doing exactly what it was built for, on day one.
- **Acceptance run**: mgu's real draft (`~/c/oa/marin-gcs-usage/dscrd/moojin-dm/`, the session formerly drafted as `moojin-kim.md`) served offline through the vendored bundle and CIC-verified — Discord-faithful render (code chips, bullets, bold, auto-linked URL), lint panel clean, `mtime` a string. Read-only against the real session; the edit/conflict paths were verified earlier on both sides (thrds stub CIC 2026-08-26; discord-agent's e2e + built-bundle run against this repo's `server.py` 2026-08-28).

Phase 4 (bot staging push) stays deferred per its own terms. Slack preview remains a future spec.

## Non-goals

- Slack preview: wanted eventually (mrkdwn diverges from markdown worse than Discord does), but a separate grammar and a separate spec. The `preview` verb's server/bundle plumbing should be platform-parameterizable so Slack drops in, but no Slack renderer work here.
- Posting anything to Discord. No tokens, no network.
- WYSIWYG editing semantics beyond "edit source, see render".

## Acceptance

- `thrds discord preview` in a discord session opens a page rendering the doc Discord-faithfully (verified against the calibrated corpus), offline.
- Editing in the page and saving updates the `.md`; a dirty-on-disk conflict is refused with a merge prompt, not clobbered.
- Corpus-alignment test green: `DiscordLinter` agrees with the vendored corpus on its declared rule families.
- `BUNDLE_PROVENANCE` records exactly which parser build is vendored.
- mgu's real draft (`~/c/oa/marin-gcs-usage/thrds/dscrd/moojin-kim.md`) previews correctly end-to-end — the live use case this whole thread started from.
