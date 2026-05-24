# thrds (TypeScript)

Declarative thread sync for Slack, Discord, and Bluesky — TS impl.

> The Python impl lives on the [`py` branch][py-branch] (and on PyPI as [`thrds`][pypi]).
> See [`specs/typescript-port.md`][port-spec] for the porting plan.

## Status

**Pre-release** — `core` (diff/edit/post/delete algorithm) is ported and tested. `slack`, `linked` ports are forthcoming.

## Install

```bash
pnpm add thrds       # not yet published
```

## Usage

```ts
import { sync } from "thrds";
import type { Thread, ThreadClient } from "thrds";

const client: ThreadClient = /* Slack/Discord/etc. impl */;
const thread: Thread = { messages: ["OP", "Reply 1", "Reply 2"] };

const result = await sync(client, thread, /* threadId */ undefined);
// result.actions: Action[]  (POST / EDIT / DELETE / SKIP per slot)
// result.messageIds: string[]
// result.threadId: string
```

## Sync algorithm

Given desired messages `M` and existing thread messages `N`:

1. **Delete** extras from the end (backwards — replies before OP)
2. **Edit** overlapping messages where content changed (skip unchanged)
3. **Post** new messages at the end

Foreign (non-editable) messages — e.g. human replies in a bot thread — are automatically skipped. The sync only operates on the bot's own messages (`message.editable === false` ⇒ preserved in place), leaving everyone else's untouched.

## Test fixtures

`tests/fixtures/sync.json` is shared with the Python impl as the cross-language contract for the sync algorithm. To pull updates from the `py` branch:

```bash
git checkout py -- tests/fixtures/
```

[py-branch]: https://github.com/runsascoded/thrds/tree/py
[pypi]: https://pypi.org/project/thrds
[port-spec]: https://github.com/runsascoded/thrds/blob/py/specs/typescript-port.md
