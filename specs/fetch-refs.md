# `fetch` + remote-tracking refs: give Slack a merge base

Slack channels are pseudo-remotes: mutable shared state with no version tracking, no compare-and-swap, and no way to ask "has this changed since I last looked?" other than reading it. A thrds session dir is already a git repo, so we can supply the missing half locally — a ref recording *our view* of each pseudo-remote, exactly as `origin/main` records our view of a real one.

Modeled on [`ghpr`]'s `refs/ghpr/remote` + `ghpr fetch` ([`d119a07`]), which solved the same problem for GitHub PRs. Two things below go beyond it; they're marked.

## Why

Three concrete defects today, all the same root cause — nothing records what Slack last looked like:

1. **`pull` clobbers.** `cli.py:806` is an unconditional `tf.path.write_text(text)` with no dirty-worktree check, followed by `_autocommit`. Uncommitted local edits to a thread file are destroyed silently, recoverable only from the nested repo's reflog by someone who noticed.
2. **`diff` can't say which side moved.** It reports local-vs-Slack and calls it "what `pull` would change", but a hunk is equally "Slack edited this" and "you edited this and `pull` is about to eat it". Those want opposite actions. With a base, the same comparison becomes three-way: local-only (push it), remote-only (pull it), both (conflict).
3. **`push` has no upstream gate.** It writes to Slack without ever establishing that Slack still looks the way we last saw it. This is the guarantee a real git server gives for free — a non-fast-forward rejection — and it is the one we most want, because our writes include `chat.delete`, which Slack cannot undo. The 2026-08-19 promote incident permanently lost approval reactions.

## Refs

Under `refs/remotes/<platform>/`, not `refs/thrds/`:

    refs/remotes/slack/staging     what staging says (draft threads)
    refs/remotes/slack/prod        what each target says (posted threads)
    refs/remotes/slack/remote      the composite `pull` reconciles onto

Reason for the namespace: git only auto-enables reflogs for `refs/heads`, `refs/remotes`, `refs/notes`, and `HEAD`, so a ref under `refs/thrds/` would silently have no history of its own movements — and "what did Slack look like before this fetch" is precisely what we want recoverable. Under `refs/remotes/` we also get `git branch -r`, `git log slack/staging`, and `git diff slack/staging HEAD` for free, all reading exactly as what they are. Cost: `git remote` won't list `slack`, since there's no config section for it (deliberately — a URL-less remote would break `git fetch --all`). `<platform>` generalizes to `discord/`, `bsky/`.

None is ever checked out.

**The composite is the merge base; the sources are for diagnosis.** Never rebase onto staging or prod individually: drafts exist only in staging, posted threads are canonical in prod (`e62fa81`), so each source tree is partial. `slack/remote` is their union under the precedence `pull` already applies, and it's the only ref for which `slack/remote..HEAD` means "local commits Slack hasn't seen". The sources are nearly free — `fetch` already makes both API calls separately (`pull_threads_staging` / `pull_promoted_threads`) — and they buy `git diff slack/staging slack/prod -- 04-cuda-graph.md`: *has the frozen staged copy drifted from what's live?*, which is the promote-incident class of question.

The gist stays a genuine remote (`refs/remotes/g/main`). So: `g` is the mirror we push, `slack/*` is the pseudo-remote we fetch.

## `fetch`

    slck fetch [-n] [--prod|--staging]

Build a commit whose tree is Slack's current state, parented on the current ref; advance the ref. Never touches the working tree, index, or branch — the snapshot is built in a throwaway worktree under the git dir (`.git/thrds/fetch-wt`), which is also what makes `-n` genuinely side-effect-free. `-n` reports without advancing.

**Nop semantics.** The comparison is content equality of the projected tree: stage the rebuilt files, and commit only if `git diff --cached` is non-empty. So `fetch` is a nop iff Slack projects byte-identically to what we last recorded, and otherwise the ref moves and there is an implied commit whose diff *is* the remote delta — `git diff slack/remote@{1} slack/remote`, or `git show slack/remote`. That is the model asked for, and it's what ghpr already does.

One honest limit: content equality means a remote change that projects to the same markdown (an edit reverted before we looked, whitespace Slack normalizes away) is indistinguishable from no change. That's the right call — content is our unit of concern — but it is not the same as a version token, and no Slack API offers one.

**Bootstrap** for sessions predating this: don't assume `HEAD == remote` the way ghpr's `ensure_remote_ref` must. We can just look. The first fetch builds the observed tree and commits it *parented on HEAD*, so `slack/remote..HEAD` is empty (nothing to replay, correct for a bootstrap) and history stays connected. Honest from the first command, no warning banner needed.

## `push`: the upstream gate

**New — ghpr does not do this, and should.** Its `push` only advances the ref (`push.py:478`, gated on "was anything held back"); nothing refuses when the remote moved underneath.

`push` begins with an implicit `fetch`. Then:

- **If the fetch moved the ref**, Slack has state we haven't merged. Refuse, naming the threads: `03-cw-mpu.md changed in staging since your last pull`. `slck pull` to reconcile, or `--force` to overwrite deliberately.
- **If the ref is not an ancestor of `HEAD`**, local history diverged from the recorded remote (a `reset --hard` past it, a rebase that dropped it). Refuse. Cheap to check, rare, and silent corruption otherwise.

**Detect per thread; refuse the whole push.** Compute the refusal set precisely — `git diff --name-only <old-ref> <new-ref>` ∩ the paths this push would write — because that's what makes the error message actionable (`03-cw-mpu.md moved in staging; 01, 02 are clean`). But having computed it, refuse *everything*, rather than pushing the clean threads and holding back the moved one.

Partial application is wrong here for two reasons. The weaker one is least surprise: `slck push` means "make Slack match my working tree", and half-doing that while reporting success has no honest answer to "did my change land?". The stronger one is that threads are not actually independent — `refs.py` resolves `[text](#slug)` cross-refs between them, so pushing `01` while holding back `03` can publish a link whose target doesn't yet say what the link promises. A commit that touches both threads means both, and the gate shouldn't quietly decide otherwise.

The escape hatch is explicit, not implicit: `slck push <slug>...` to deliberately scope a push to a subset, and `--force` to overwrite a moved thread on purpose. Both are the user saying "yes, partially" — which is fine — rather than the tool assuming it.

## `push`: post-write verification

**Also new.** ghpr advances the ref to `HEAD` — i.e. to *assumed* remote state. For thrds that's wrong twice over.

First, our writes are a *projection*, not a copy: Slack normalizes mrkdwn, resolves emoji, adds unfurls, and holds things thrds can't represent (foreign messages, reactions). So `HEAD` is never exactly what Slack now contains, and recording it as such guarantees the next `fetch` reports a spurious remote-only delta — which then makes the nop-on-unchanged property, and therefore the push gate built on it, cry wolf on every cycle.

Second, recording assumed state is the same error ghpr's "don't advance if anything was held back" rule guards against, generalized: the ref's whole value is that it's a truthful record.

So `push` ends by re-reading the threads it touched and advancing the ref to the **observed** projection. Cost is one `conversations.replies` per touched thread — proportional to the write, not to the session. If observed differs from intended, say so on stderr (that's the interference signal) but still record observed. Truth over intent; the delta shows up in the next `diff` either way.

This makes "a fetch right after a push is a nop" actually hold, which is what the gate needs.

## TOCTOU

There is no server-side CAS, so we cannot be atomic; we can only narrow windows and make detection reliable after the fact.

- **Narrow:** the implicit fetch immediately precedes the write, so the window is the push's own duration rather than "since you last ran something".
- **Detect:** post-write verification catches interference that landed inside the window, after the fact but before it can compound.
- **Refuse, where it's unrecoverable:** `DELETE` gets a stricter rule than the rest. Slack deletion is irreversible and takes reactions with it, so never delete a message whose current text differs from what the plan read — re-read immediately before the call and abort that action if it moved. `EDIT` is recoverable from our own history, so it gets the ordinary gate.

Cheap, and it's a second line of defense under the `only_ids` scoping fix — that one addressed a logic bug, this addresses the race.

## `pull` = fetch + reconcile

    slck pull [-n] [-m rebase|merge|overwrite]

Default `rebase`, overridable via `git config thrds.pullMode`. `slack/remote..HEAD` is the set of local commits Slack hasn't seen, so a rebase replays exactly those onto the fetched state.

`rebase` and `merge` **abort on a dirty working tree** rather than overwriting it, printing the `git rebase --onto <new> <old> <branch>` needed to finish by hand — the ref is already advanced, so manual reconciliation is always available. `overwrite` is today's behavior, retained explicitly and reporting how many local commits it discards plus the SHA they're recoverable at.

This is the fix for defect 1.

## `diff`, three-way

With a base, `slck diff` classifies each thread instead of merely showing a delta:

| `local vs base` | `remote vs base` | reported as |
| --- | --- | --- |
| — | — | unchanged (silent) |
| changed | — | local-only — `push` will send it |
| — | changed | remote-only — `pull` will apply it |
| changed | changed | **conflict** — both moved since the base |

Same output format as today (`diff_texts`, `NN-slug.md (local)` / `(slack)` headers); the classification is a one-line prefix per thread. This is the fix for defect 2, and it's the payoff that most directly upgrades work already shipped ([[per-thread-diff]]).

## thrds-specific divergences from ghpr

1. **`thrds.json` stays out of the mirrored tree.** ghpr's whole tree is content; ours carries state. Rebasing local commits onto a fetched `thrds.json` means merge conflicts in JSON, far worse than conflicts in prose. The refs track `*.md` only; state reconciliation stays as it is.

   Known gap, named rather than papered over: chrome-driven retargets and renames land in `thrds.json`, so those remain clobber-prone. Revisit if it bites.

2. **`promote` needs the same treatment as `push`.** It's per-thread and it's the verb that touches prod, so: implicit fetch of that thread's target, per-thread gate, post-write verification, and advance `slack/prod` only for the slug it converged.

3. **Legacy single-doc sessions** get the same refs with a one-file tree, or are simply excluded until migrated. Excluded is fine — `migrate` exists.

## Phasing

1. `refs.py` (read/set/ensure, per-platform namespace) + `fetch` with `-n`, scratch worktree, nop semantics, honest bootstrap. Independently useful: `git diff slack/remote HEAD` the moment it lands.
2. `diff` three-way classification. Small, once the base exists.
3. `pull` reconcile modes + dirty-worktree abort.
4. `push` gate + post-write verification.
5. `promote` gate; `DELETE` pre-flight content check.

[`ghpr`]: https://github.com/runsascoded/ghpr
[`d119a07`]: https://github.com/runsascoded/ghpr/commit/d119a07
