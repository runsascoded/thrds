# `slck diff` for per-thread sessions

`diff` is still legacy-single-doc only. On a per-thread session it currently takes a thread-file path, falls into the DOC_PATH code path, and produces garbage (compared `05-grug-deleted.md` against an empty doc — looked like Slack had deleted everything).

## Desired

- `slck diff` (no args, per-thread session): for each thread with a `staging_ts`, render the Slack side back to local md (same conversion `pull` uses) and diff against the working-tree file. Output unified diffs, one `--- NN-slug.md` header per changed thread; print nothing for unchanged threads; exit 0 always (it's a report, not a gate).
- `slck diff <slug-or-file>`: same but restricted to one thread.
- Direction convention: local WT is `a`/`---`, Slack is `b`/`+++` — "what `pull` would change".

## Relationship to `pull -n` (asked 2026-08-19)

`pull -n` *dumps* the Slack side; it doesn't compare. `diff` is the comparison (WT vs Slack). Post-`pull`, `git diff HEAD~1` shows what Slack changed — but that's after the write; `diff` answers it before.

## Also

Legacy `diff` invoked inside a per-thread session should error like `pull --prod` does (`Per-thread sessions …`), not silently mis-compare.
