# Spec: TypeScript port of `thrds`

> Status: **draft** (2026-05-24, from `~/c/hccs/ctbk` session)
>
> Origin: ctbk's Cloudflare Worker (`gbfs/api/src/alerts.ts`) needs the
> firing-OP-then-resolved-reply pattern that `SlackClient.sync()` provides.
> The CFW is TS, `thrds` is Python — so port the Slack subset to TS,
> publish to npm, adopt cross-runtime.

## Why

Multi-runtime presence. ctbk has Python (CLI + GHA workflows) and TS
(Cloudflare Workers, www frontend). Other projects (Open-Athena's
marin-discord is JS, hudcostreets/nj-crashes is Python) repeat the same
pattern. A single declarative thread-sync API across languages is
worth a parallel impl.

`npmjs.com/package/thrds` is unclaimed.

## Repo restructure (precondition)

Move the existing Python layout to `python/`, mirror with `ts/`:

```
~/c/thrds/
├── python/                  # was top-level
│   ├── pyproject.toml
│   ├── thrds/               # package
│   │   ├── __init__.py
│   │   ├── core.py
│   │   ├── slack.py
│   │   ├── discord.py
│   │   ├── bsky.py
│   │   ├── linked.py
│   │   └── protocol.py
│   └── tests/
├── ts/                      # new
│   ├── package.json         # name: "thrds"
│   ├── tsconfig.json
│   ├── vitest.config.ts
│   ├── src/
│   │   ├── index.ts
│   │   ├── core.ts          # Thread, Message, sync algorithm, types
│   │   ├── slack.ts         # SlackClient
│   │   └── linked.ts        # LinkedThread, sync_linked
│   └── tests/
│       ├── sync.test.ts
│       ├── linked.test.ts
│       └── foreign-messages.test.ts
├── specs/
│   ├── done/
│   └── typescript-port.md   # this file → done/ when impl lands
├── LICENSE
└── README.md                # update with both langs
```

Notes:
- The current uv `pyproject.toml` keeps working at `python/pyproject.toml`
  (just relocate). `cd python && uv sync` from there.
- The TS `package.json` publishes from `ts/` — `npm publish` (or
  `pnpm publish`) runs from that subdir.
- Top-level `README.md` documents both langs; each subdir can keep its
  own focused README.
- GHA CI gets two job legs (`python-tests`, `ts-tests`).

## v0.1 scope (TS)

Match the Python API as closely as idiomatic in TS. Out-of-scope for v0.1:
Discord client, Bluesky client. They can land later.

In scope:

1. **`core.ts`** — `Thread`, `Message`, `Action`, `ActionType`, `SyncResult`,
   `SyncOptions`. Pure types + the diff algorithm (`computeActions(desired, existing, ...)`).
2. **`slack.ts`** — `SlackClient` class with:
   - constructor: `{ token, channel, username?, iconEmoji? }`
   - `list(threadTs?): Promise<ExistingMessage[]>`
   - `post(text, opts?)`, `edit(ts, text)`, `delete(ts)`
   - `sync(thread: Thread, opts?): Promise<SyncResult>`
   - Rate-limit handling (429 + `Retry-After`), `pace` / `jitter` opts
   - Foreign-message preservation (the same rule the Python impl uses:
     skip messages where `bot_id` ≠ ours, or `user` ≠ our user_id)
   - Orphan guard on `delete()` — refuse to delete a thread parent that
     has non-bot replies; raise `OrphanedRepliesError`
3. **`linked.ts`** — `LinkedThread`, `Section`, `syncLinked()`. The
   two-phase placeholder→edit flow. (User explicitly asked for this in
   v0.1.)
4. **Tests** — port `test_sync.py`, `test_linked.py`,
   `test_foreign_messages.py`, `test_orphan_guard.py` to vitest. Mock
   the HTTP layer; no live Slack required.

API style: idiomatic TS. snake_case → camelCase (`iconEmoji`,
`threadTs`, `syncLinked`). `enum ActionType` or string literal union.
`async`/`await` throughout.

## v0.1 non-goals

- Discord, Bluesky clients (port later if/when a TS consumer appears)
- Splitting Python repo to its own `thrds-py` package — keep the
  Python `thrds` package name as-is on PyPI.
- Behavior-level deviations from Python — match `SyncResult.actions`
  semantics exactly so the test fixtures port cleanly.

## Distribution

- **npm**: publish `thrds` from `ts/` — `pnpm publish` or `npm publish`
  with `prepublishOnly: tsc`. Workspace name = `thrds`.
- **PyPI**: existing `thrds` package keeps shipping from `python/` —
  no PyPI rename.
- **GH Actions**: two release workflows, one per ecosystem. Both
  triggered by `vX.Y.Z` tag pushes; if you want truly parallel
  releases, tag suffixes (`vX.Y.Z-py`, `vX.Y.Z-ts`) or split-version
  numbering. Simplest: one tag → both publish.

## Wire-up at ctbk

Once published:

```bash
cd ~/c/hccs/ctbk/gbfs/api
pnpm add thrds
```

Then `gbfs/api/src/alerts.ts` refactor:

```ts
import { SlackClient, Thread } from 'thrds';

const slack = new SlackClient({
  token: env.SLACK_BOT_TOKEN,
  channel: 'C0B5MKF28NP',
  username: 'ctbk-bot',
  iconEmoji: ':bike:',
});

// Per-rule state extended with `threadTs`:
// interface AlertState { firing: Record<ruleId, { firingSince: string; threadTs: string }> }

// On firing: post OP, store thread_ts
// On still-firing: no-op (could later: edit OP with "still firing X min")
// On resolved: sync Thread([firingText, resolvedText]) with stored thread_ts
```

The `state.json` schema will need a v2 migration to add `threadTs`; safe
to detect missing field and post un-threaded for legacy entries.

## Open questions

1. **`vitest` vs `node:test`?** Vitest is more familiar to ctbk's existing
   stack and gives jest-style assertions; `node:test` is zero-dep. Start
   with vitest.
2. **Workspace tooling**: pnpm? npm? Match author's preference. ctbk uses
   pnpm — suggest the same here.
3. **Tagging strategy** for releases — defer until first cut.

## References

- Python source: `python/thrds/slack.py`, `core.py`, `linked.py`
- Python tests: `python/tests/test_sync.py`, `test_linked.py`
- Consumer that motivated this: `~/c/hccs/ctbk/gbfs/api/src/alerts.ts`
  (CFW alerting cron — firing/resolved transitions)
- Task tracking in ctbk: task #57
