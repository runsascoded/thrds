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
