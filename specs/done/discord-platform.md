# thrds: platform subgroups (Slack + capture-only today; Discord next)

**Phase 1 landed 2026-08-14**: `thrds slack …` + `thrds capture …` subgroups,
`platform` field on `SessionState`, mismatch guard. This alone unblocks
`$c/voice` trajectory capture. Phase 2 (Discord staging push + lint) is
scoped below but not implemented — a separate future spec.

## Original goal (RW, 2026-08-13)

Let a `thrds` session target **Discord** (and, more generally, capture a
message's iteration history to a gist **without** any platform posting) — so
Discord drafts get the same gist-mirrored trajectory that Slack drafts already
do, for use as `$c/voice` examples.

## The Discord asymmetry that shapes the design

`discord.py` posts with a **bot token** → a posted message appears as the
**bot**, not as the user. Discord **prohibits user-token automation** (self-bots
violate ToS), so — unlike Slack's user-OAuth — there is **no clean "post as
me."** Therefore:

- **Prod delivery for Discord = copy-paste** (the user pastes the final MD
  themselves). This is the intended workflow, not a limitation to engineer
  around.
- **The bot is for staging/preview only**: post to a **private server/channel
  of your own** to see exact rendering before pasting.

## Design decisions (finalized during phase 1 implementation)

The original spec proposed `thrds init DOC.md --platform slack|discord|bsky|none`
(one flag on a shared verb surface). During implementation, we pivoted to
**platform-as-subcommand** for two reasons:

1. **Slack, Discord, and (future) Bsky are different enough** — different
   provisioning models (Slack has staging PC / prod channel; Discord has bot
   staging + copy-paste prod; Bsky has neither), different config, different
   verb sets. A single flag would force each platform through a Procrustean
   `push`/`pull`/`diff`/`archive` shape that doesn't fit half of them.
2. **The existing `thrds slack …` CRUD group** (from
   `specs/done/slack-crud-cli.md`) already established the subgroup pattern.
   Extending it to include the session verbs was a natural fit and let CRUD +
   session verbs share the same namespace.

**Naming**: `capture` (not `none`, not `gist`). Rationale: "capture" is
about the intent (no platform target), keeping `--no-gist` orthogonal (draft
locally without a gist mirror is still a valid mode inside `capture`).
`gist` would lock the name to the mechanism.

**Platform is stamped implicitly** by which `init` subcommand ran, not via a
flag. `state.platform` is validated (`slack|discord|bsky|capture` today) and
every platform-group verb calls `_load_state(expected_platform='<name>')` to
guard against mis-platforming — running `thrds slack push` inside a
capture-inited session errors with a clear "wrong platform" message rather
than blowing up deep inside `SlackClient.sync_doc_staging`.

**No BC on the old top-level verbs.** `thrds init/push/pull/…` at the top
level are gone. `.thrds-rc` ships sample aliases (`tsi`, `tsp`, `tci`, etc.)
that users can adopt to keep short-form invocations.

## Phase 1: what landed (2026-08-14)

- `thrds slack {init,push,pull,diff,archive,open,list-sessions,recover}` —
  the session verbs, moved from top-level under the `slack` subgroup.
- `thrds slack {history,thread,rm,post,edit,permalink}` — CRUD verbs,
  unchanged (already in that subgroup from `slack-crud-cli.md`).
- `thrds capture {init,push,open}` — new subgroup: gist-mirrored trajectory,
  no platform target. `init` scaffolds the session; `push` commits + pushes
  to the gist; `open` opens the gist URL.
- `SessionState.platform: str = "slack"` with `VALID_PLATFORMS =
  ('slack', 'discord', 'bsky', 'capture')`. `__post_init__` validates.
  Legacy state files (pre-0.6) backfill to `'slack'` on load.
- `_do_init()` + `_print_init_completion()` shared helpers.
- `_load_state(expected_platform=...)` mismatch guard.
- `.thrds-rc` rewritten: `ts*` = slack, `tc*` = capture. Two CRUD verbs
  renamed to avoid collisions with session verbs (`tspo`=post, `tsrm`=rm; the
  single-letter slots go to the more-used session `push`/`recover`).
- README `## CLI` section rewritten around the subcommand model.
- Tests: `test_capture_cli.py` (14 tests) + 5 new `test_state.py` tests +
  updates to `test_cli.py` for the new invocation syntax.

## Phase 2c (landed 2026-08-14): Discord lint + render, no live push

Picked **option (c) — lint-only**. Rationale: (a)/(b) require porting Slack's
doc-level API to `DiscordClient` for a benefit (rendering into a private
staging channel to preview before paste) that a good local linter mostly
captures without any Discord API involvement. Bot-token config and a "staging
channel" concept can land later if the render-preview workflow proves
insufficient.

What landed:

- `thrds discord {init,render,lint,open}` subgroup — no `push` verb.
- `thrds.lint` module with a `DiscordLinter` class + `LintIssue` / `LintReport`
  dataclasses. Reusable across future platform linters.
- Lint rules (originally three; masked-link rule was wrong — see
  `specs/done/discord-masked-links-render.md` — and was removed 2026-08-14):
  - ~~**masked links** (`[text](url)`)~~ — **removed**: they do render as
    hyperlinks in normal Discord user messages (with click-through warning
    for untrusted domains) since Discord's 2023 markdown update.
  - **markdown tables** don't render; use a code block or bullets.
  - **raw `@name`** doesn't ping; needs `<@user_id>`.
- `thrds discord render` prints the doc's MD to stdout and auto-runs lint
  (warnings → stderr). `--no-lint` bypasses.
- `thrds discord lint` is standalone; prints `<path>: no issues` if clean.
- `.thrds-rc` aliases: `tdi`/`tdr`/`tdc`/`tdl`/`tdo` (with `tdc` set to
  `thrds discord render | pbcopy` for the macOS clipboard flow).
- Tests: 17 in `test_lint.py` (rule coverage: masked-link, table, mention,
  code-fence exclusions, punctuation handling, report shape) + 10 in
  `test_discord_cli.py` (init, render, render-with-lint, lint standalone,
  open, platform-mismatch guard both directions).

Deferred to a future phase (if the lint-only loop proves insufficient):

- **Discord staging push** — (b) or (a) from the original scope. Bot-token
  config from env (`DISCORD_TOKEN`), staging `guild`/`channel` per env or
  per session, `push` posts the doc's threads via `sync_linked` for
  render-preview inside a private Discord channel.

## Non-goals

- Posting **as the user** in Discord (ToS). Prod stays copy-paste.
- Multi-thread orchestration for Discord — single top-level messages are
  the norm; `create_thread` exists on `DiscordClient` but isn't the default
  flow.

## Status

Phase 1 (landed):
- [x] `platform` field on `SessionState` + validation + legacy backfill
- [x] `thrds slack …` subgroup (session verbs moved under it)
- [x] `thrds capture …` subgroup (init/push/open)
- [x] Shared `_do_init` + `_print_init_completion` helpers
- [x] Platform-mismatch guard via `_load_state(expected_platform=...)`
- [x] README + `.thrds-rc` rewritten for the subgroup model
- [x] Tests: `test_capture_cli.py` (14) + platform-field tests (5)

Phase 2c (landed 2026-08-14):
- [x] `thrds discord {init,render,lint,open}` subgroup (option `c` — no push)
- [x] Discord MD-compat linter (tables / raw `@name`; masked-link rule
      removed 2026-08-14 — see `specs/done/discord-masked-links-render.md`)
- [x] `render` surface + `.thrds-rc` `td*` aliases (incl. `tdc = render | pbcopy`)

Also landed 2026-08-14 (same commit family):
- [x] `thrds bsky {init,render,lint,open}` subgroup — same phase-2c shape
- [x] `BskyLinter` — 300-char post-length + masked-link rules

Deferred (a later spec, if the lint-only loops prove insufficient):
- [ ] Discord staging push — (b) MVP or (a) full-parity from original scope
- [ ] `thrds bsky push` — actual atproto posting via `BskyClient`
