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

## Outcome (implemented 2026-08-19)

All of the above, plus the decisions the spec didn't settle:

**The local side is the file verbatim, not `serialize_thread(parse(file))`.** `diff_docs` canonicalizes both sides deliberately — it compares *content*, so a formatting variant that round-trips to the same canonical form shouldn't show up. That's the wrong call here: `pull` overwrites the file, so local formatting it would rewrite genuinely is a pending change, and canonicalizing first would hide it. Hence a new `md.diff_texts` (which `diff_docs` now delegates to) rather than reuse.

**Headers are `NN-slug.md (local)` / `NN-slug.md (slack)`** — satisfying the "one `--- NN-slug.md` per changed thread" requirement while naming the direction, since `a`/`b` isn't self-evident when neither side is a commit.

**`<slug-or-file>` limits the fetch too**, via a new `slugs=` filter on `pull_threads_staging` → `_pull_doc(only=)`. The filter applies to *fetching* only; `thread_ts_by_slug` stays whole, because it's the map `_reverse_cross_refs` resolves against — narrowing it would leave a link to an unfetched sibling as a raw permalink and report that as a change.

Edge cases, each with a test:

| case | behavior |
| --- | --- |
| thread file with no `staging_ts` | skipped in the all-threads case (`pull` wouldn't touch it); a stderr note when named explicitly |
| `staging_ts` with no local file | diffed as an empty local side — "pull would restore it" |
| Slack OP deleted (thread returns 0 messages) | empty remote side, so the diff reads as the file going away, not "one blank line remains" (`serialize_thread` of an empty thread is `'\n'`) |
| session with nothing staged | `No staged threads to diff.` on stderr |
| unknown slug | `UsageError` listing available slugs |
| `--prod` on a per-thread session | `UsageError`, mirroring `pull --prod` |

**Promoted threads diff against prod, not staging.** `pull` gained `pull_promoted_threads` (`e62fa81`) while this was being written: for a `posted` slug, prod is canonical and overrides the frozen staging copy. `diff` has to apply the same precedence or it reports a hand-edit made at the target as a change `pull` would undo — so it fetches both and overlays, including the fallback to staging when the prod copy is unfetchable (a deleted message, as with `cuda-graph`). `pull_promoted_threads` got the same `slugs=` filter.

Verified live against both sessions (`cw-quickwins`, `trainium-2026-07-31`): both report clean, and a deliberately perturbed `05-grug-deleted.md` — the file from the original bug report — diffs correctly under both `diff` and `diff 05-grug-deleted.md`. Caveat on the prod-precedence path: `cw-quickwins`' staging and prod copies currently agree, so live it only exercises the code path, not the divergence; the `cuda-graph` fallback (posted, prod copy deleted) is live-exercised, and divergence is covered by unit tests.

Not done, deliberately: `diff` reports content only. Retargets and renames pending in a Slack-side chrome edit are `pull`'s business and would cost extra API calls to surface here.
