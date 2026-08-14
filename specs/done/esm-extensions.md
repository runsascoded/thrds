# Spec: Fix ESM imports in `@rdub/thrds`

> Status: **done** (2026-08-14) — resolved differently than originally proposed.
> Original draft 2026-05-24 (from ctbk session attempting to adopt
> `@rdub/thrds@0.1.0` in a Cloudflare Worker).

## Problem (unchanged)

`thrds@0.1.0` on npm shipped with extensionless relative imports in its dist
`.js` files:

```js
// node_modules/thrds/dist/index.js
export { EditRateLimited, OrphanedRepliesError, sync } from "./core";
```

Because the package is `"type": "module"`, Node (≥16) and strict vitest
resolvers fail:

```
Error: Cannot find module '/…/node_modules/thrds/dist/core'
imported from /…/node_modules/thrds/dist/index.js
```

Hit during ctbk's `gbfs/api/` worker adoption.

## Resolution (took a different route)

Rather than the source-level fix (adding `.js` to every relative import), the
project switched to **`tsup` for single-file ESM bundling** — commit
`101efc3 Build with tsup: single-file ESM bundle; fixes Node ESM extension
issue`. `dist/index.js` is now one bundle with zero internal relative
imports; nothing for Node's ESM resolver to trip over.

Additional related work that landed since 0.1.0:

- `41ce7fb Rename npm package to \`@rdub/thrds\`` — namespaced under `@rdub`;
  the old un-namespaced `thrds` package is deprecated at 0.1.0.
- `68abd19 CI: publish built artifacts to \`ts-dist\` branch on every push` —
  consumable via `pnpm add ryan-williams/thrds#ts-dist` for pre-release testing.
- `0a0b7d8 Fix npm-dist action ref` — action version pin.

## Release status

- **`package.json` version: `0.1.1`** — bumped, not yet published.
- **`v0.1.x` tags exist locally but predate the `ts-v*` scheme** — they were
  Python-package tags before the `py-v*` / `ts-v*` split. They do NOT trigger
  the TS release workflow.
- **`.github/workflows/release.yml` triggers on `ts-v*.*.*` tags** — no such
  tag exists yet, so `@rdub/thrds@0.1.1` has never shipped to npm. `npm view
  thrds versions` still returns `0.1.0` (the un-namespaced package); `npm
  view @rdub/thrds versions` returns 404.

To publish `@rdub/thrds@0.1.1`:

```bash
cd ~/c/thrds/wt/ts
git tag -a ts-v0.1.1 -m "Release @rdub/thrds@0.1.1 (tsup bundle + npm namespace)"
git push r ts-v0.1.1
# release.yml runs on tag push → OIDC-authenticated `pnpm publish`
```

## ctbk-side wire-up after `0.1.1`

```bash
cd ~/c/hccs/ctbk/gbfs/api
pnpm add @rdub/thrds@^0.1.1    # note the new @rdub/ scope
pnpm test    # 18 alerts tests + the rest should pass
```

`gbfs/api/src/alerts.ts` was already refactored locally to use
`SlackClient.sync()` + the `FiringEntry` state shape (firing OP + resolved-as-reply);
just held back until this thrds release lands.

## Why the tsup approach was preferred (retrospective)

Adding `.js` to every source import is more verbose (touches every relative
import in every file, forever), and requires TS `moduleResolution` to be one
of the modes that tolerates the `.js`-on-`.ts` form (`NodeNext` or
`Bundler` — the current `tsconfig` uses `Bundler`, which would tolerate it,
but doesn't emit anything different). Bundling side-steps the whole
question at the dist boundary.

## References

- ctbk consumer that triggered this: `~/c/hccs/ctbk/gbfs/api/src/alerts.ts`
- ctbk task: #57 (unblocks on this release)
- Node ESM spec: https://nodejs.org/api/esm.html#mandatory-file-extensions
