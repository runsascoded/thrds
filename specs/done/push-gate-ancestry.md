# The push gate must ask a standing question, not compare successive observations

From the ghpr session (`~/c/ghpr`, session `ed8cbe29`), written 2026-08-24 in response to `~/c/ghpr/specs/merged-into-thrds.md`:

> Whatever you know about why `merge-base --is-ancestor` was the durable form — especially the live catch where the second push passed — is worth writing down before this session ends, because it is the highest-value thing that hasn't been transferred.

This is that writeup. It is item 2 in the unification queue in `remotes-model.md` ("Landed 2026-08-23"), and it is a live bug on the thrds side today — `thrds/cli.py:_gate_push` self-clears, and the trace is below.

## The bug, as it actually happened

ghpr's first push gate was written as *"did this fetch see anything new?"*. `_gate_upstream` fetched, compared the fetched snapshot against the recorded ref, and refused if they differed. It also — correctly, and this is the part that bites — advanced the ref on refusal, because a refusal is still an observation and `git diff HEAD <ref>` is how you see what caused it.

Those two facts are incompatible. The refusal writes down the very thing the next run compares against:

```
# remote edited out from under a local HEAD that predates the edit
$ ghpr push        # fetch sees a delta vs. the ref → refuse (exit 1) → ref := observed
$ ghpr push        # fetch sees no delta vs. the (just-advanced) ref → PASS (exit 0)
```

The second push overwrote the remote edit. Nothing about the danger had changed between the two commands — HEAD still didn't contain the remote's state — but the gate's *memory of having looked* had. A gate that disarms itself by firing is worse than no gate, because the operator reads the first refusal as "ghpr is protecting me" and the retry as "I must have fixed it."

**This was caught live, not by the suite**, on the real PR `runsascoded/ghpr#6`. The suite was green: it asserted the refusal, once. That is the transferable testing lesson — **a gate test that pushes once tests nothing**. Every gate needs a "run it twice, it still refuses" case; `tests/test_fetch_pull.py::TestPushGate::test_re_running_push_still_refuses` is ghpr's, and it is the single most valuable test in that file.

(Methodology footnote from the same debugging session: `ghpr push | tail; echo $?` reports `tail`'s status, not the gate's. It briefly made the fix look like it hadn't worked. Use `${PIPESTATUS[0]}`, or don't pipe.)

## Why ancestry is the durable form

The fix is not "stop advancing the ref on refusal" — that would cost the diff. It is to ask a question whose answer doesn't depend on when you ask it:

```python
if proc.check('git', 'merge-base', '--is-ancestor', snap.sha, 'HEAD', log=None):
    return
```

"Does HEAD contain the remote's current state?" is a **standing property of two states**. "Did something change since I last looked?" is a **difference between two events**. Only the first survives being asked twice, because only the first has no term that the act of asking moves.

Note what the ancestry test does under the ref advance that broke the old version: the ref moves *forward* to a commit that is not in HEAD's history, so `--is-ancestor` goes from false to... still false. Advancing the ref **strengthens** the refusal instead of clearing it. That is why ghpr can keep recording every observation — including the ones that end in refusal — without the record becoming a loophole. And it's why the gate clears for the right reason: a reconcile is the only thing that makes the ref an ancestor, so `pull` (rebase, merge, *or* overwrite) clears it and nothing else does.

The invariant, stated so it survives translation to a non-git remote:

> **The gate's operands must be the local state we would push from and the remote state as it is now. The last observation may not appear in the test at all — except as a merge base that only advances when the local state actually incorporates it.**

## Where thrds has the same bug

`thrds/cli.py:_gate_push` (lines 773–818) is the content-level transliteration of ghpr's *broken* version, with the same two incompatible facts in the same function:

```python
old = tracking.read_ref(session_dir, ref)
observed = remotes.observe(remote, client, state, session_dir)
snap = tracking.snapshot(session_dir, ref, remote.name, tracking.build_tree(session_dir, observed), ...)
if old is None or not snap.paths:
    return
...
hit = sorted(set(snap.paths) & would_write)
```

`tracking.snapshot` calls `update-ref` (`tracking.py:381`) and returns `paths = changed_paths(base_tree, tree)` (`:370`). So:

1. **First `slck push`** — remote moved, `snap.paths` non-empty, `hit` non-empty → `ClickException`. The ref is now at the observed tree.
2. **Second `slck push`** — `base_tree` is that same observed tree, so `changed_paths` is empty → `snap.paths` empty → **`return` at line 803**. The push proceeds and overwrites.

Identical failure, identical trigger, and equally invisible to a suite that pushes once. The composite being a side chain doesn't insulate it: the self-clearing lives entirely in the `snap.paths` term, which is a since-last-observation difference no matter what shape the refs have.

## What the durable form looks like for thrds

The `snap.paths` term is doing real work and can't just be deleted. `would_write` alone ("remote text ≠ local text") is true for every file we deliberately edited, so gating on it refuses every legitimate push. Distinguishing "differs because they edited" from "differs because we edited" genuinely requires a third state — the last common one. That third state *is* a merge base, and ghpr's ref is exactly that. The bug is not that thrds uses a base; it's that thrds' base advances on **observation** rather than on **incorporation**.

Three ways out, in increasing order of how much they buy:

**(a) Split the two roles the ref currently plays.** Keep advancing an observation ref on every look (so `diff` after a refusal still works), and gate against a separate base ref that advances only when HEAD incorporates that state — after a `pull`/reconcile, or after a successful push's `_verify_push`. Smallest diff, keeps every message and every property that currently holds. The gate formula is unchanged; only which ref feeds `base_tree` changes.

**(b) Make the composite a real ancestor.** If `pull` merged `refs/heads/upstream` into HEAD as a parent rather than leaving it a side chain, `merge-base --is-ancestor upstream HEAD` becomes literally available and the two platforms run *the same gate*, not two dialects of it. This is the option that pays off now that both live in one codebase: the unification queue's whole point is that one gate should exist. It's also the biggest behavioral change — the side-chain composite was chosen deliberately, and this reopens that.

**(c) Do nothing structural; re-observe inside the refusal path.** Rejected — noting it only so it isn't rediscovered. It fixes the symptom by making the second push re-fetch and re-compare against a stashed pre-refusal base, which is (a) with the base held in a worse place.

I'd take **(a) now, (b) as part of the refs merge**, since (a) is a bug fix that can land today and is a strict prerequisite for (b) making sense anyway.

Whichever lands, the acceptance test is fixed:

- push once → refuses;
- **push again without reconciling → still refuses** (the one that would have caught this);
- `pull` (each mode) → push succeeds;
- `--force` / `-G` → succeeds on the first try;
- after a refusal, the diff against the recorded observation still shows what changed.

## Two ghpr behaviors flagged as worth preserving in the merge

Repeating these from `merged-into-thrds.md` §"One thing you found", since they're adjacent to the same code and this file is likelier to be read when that code is touched:

- **Clone discovery** — ghpr walks parent dirs for `gh/<n>/` *and* falls back to git config (`pr.owner`/`pr.repo`/`pr.number`), so the verbs work from anywhere inside a clone. thrds' `_load_state` requires cwd to be the session dir. ghpr's is the better behavior; it's `resolve_item()` in `thrds/platforms/github/commands/fetch.py`.
- **Bootstrap ambiguity** — `bootstrap_is_ambiguous` (same file) refuses to guess when the fetch *doesn't* confirm HEAD, naming both resolutions (`pull -m overwrite` / `push`) rather than picking one. The reasoning is in its docstring: guessing "remote is ahead" reverts unpushed local work and then pushes the reversion (the original bug the ref exists to prevent); guessing "local is ahead" pushes a stale HEAD over a newer remote. thrds sidesteps this structurally via the composite overlay — the merged engine should pick deliberately rather than inherit whichever path it touches first.

## Status of the ghpr side

`~/c/ghpr` is clean at `236c9c9`, pushed, nothing in flight — so there is nothing to merge across. §2 of `converge-fetch-refs-ux.md` (three-way classification in `diff`) was never started; it's entirely thrds' now.

## Implemented 2026-08-24 (thrds) — option (a)

Landed as recommended, with the reproduction written first: `test_re_running_push_still_refuses` failed exactly as traced above (second push exit 0, `spy.synced` non-empty) before any fix.

`refs/heads/base/<name>` (`tracking.base_ref_name`) is the gate's operand; `refs/remotes/<name>` keeps advancing on every look, so the post-refusal diff is unchanged. The base advances in exactly three places, and the asymmetry between them *is* the mechanism:

- `_verify_push` — a completed push is incorporation by construction.
- `_mark_pull_incorporated`, at the end of both `pull` paths, after the reconcile lands (not next to the fetch that merely saw the divergence — that would recreate the bug through a different door).
- `_mark_incorporated_if_synced`, on any observation that comes back *already matching local*. This generalizes the rule instead of special-casing bootstrap: an observation equal to what we hold is an incorporation, since there is nothing left to pull. It is what lets `fetch`-then-`push` still gate correctly, and it is refused for an observation that differs however many times it repeats.

Acceptance, all pinned in `tests/test_push_gate.py` (18 passing, suite 1268): refuses; **refuses again unreconciled**; clears after `pull -m rebase|merge|overwrite` (parametrized — the non-conflicting case, since same-file edits on both sides are a genuine conflict and report as one); `--force` passes first try; the observation ref still records what changed across a refusal.

One deliberate cost: `git branch` now lists `base/<name>` beside `main`/`upstream`. `refs/heads/` was chosen for the auto-reflog ("when did we incorporate" is worth answering), and the alternative namespaces either lose it or pollute `git branch -r`, which must keep reading as exactly the remote list. `tests/test_fetch_cli.py` pins the new listing, and it strengthened that test: a partial fetch now visibly creates `base/staging` and no `base/prod`.

**Still open — the bootstrap horn, item 3 in the queue.** With no base recorded (fresh session, or a gist clone whose first observation disagrees with HEAD), the gate returns early and `push` proceeds as the "local wins" resolution. That is the same ambiguity `bootstrap_is_ambiguous` refuses to guess at on the GitHub side, and it is now the *remaining* hole in this gate rather than one of two. Picking one rule for both adapters is the next item.
