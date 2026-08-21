# Converge fetch/refs UX with thrds (and three upstream-able improvements)

From the thrds session (`~/c/thrds`, 2026-08-20). thrds implemented its analog of `d119a07` — `slck fetch` + remote-tracking refs for Slack channels-as-pseudo-remotes (`thrds/tracking.py`, spec `~/c/thrds/specs/fetch-refs.md`) — deliberately copying ghpr's UX surface, and found three things along the way that ghpr should adopt, plus one bootstrap edge worth testing before the 0.3.0 release.

## Already converged — keep these pinned

Anyone using both tools should be able to transfer the mental model wholesale. These are identical today; treat them as contract:

- `fetch` verb, `-n` for a side-effect-free preview that still shows what it would do.
- `pull -m rebase|merge|overwrite`, default `rebase`, overridable via `git config <tool>.pullMode`.
- The merge-base ref is named `…/remote`; `<base>..HEAD` = "local commits the remote hasn't seen".
- The ref only ever records *truthful* remote state (ghpr: don't advance when anything was held back; thrds: advance to post-write *observed* state).

## 1. `refs/ghpr/remote` has no reflog — move to `refs/remotes/` — DONE

git auto-enables reflogs (`core.logAllRefUpdates`) only for `refs/heads/`, `refs/remotes/`, `refs/notes/`, and `HEAD`. `refs/ghpr/remote` is outside all of them, so the ref keeps **no history of its own movements**: `git reflog ghpr/remote` is empty, and "what did GitHub look like before this fetch" — half the point of keeping the ref — is unanswerable. Each fetch commit's *parent* chain survives, but the reflog is what makes `ghpr/remote@{1}` and "when did it move" queries work, and what `git gc` uses to protect recent states.

Proposal: `refs/remotes/github/remote`. Also buys `git branch -r` listing it and `git log github/remote` reading naturally. There's deliberately no `[remote "github"]` config section (a URL-less remote would break `git fetch --all`) — thrds confirmed this shape works fine.

Migration is one line at `ensure_remote_ref` time: if the legacy ref exists and the new one doesn't, `git update-ref refs/remotes/github/remote <old>` + delete the old one.

(thrds uses `refs/remotes/slack/{staging,prod,remote}` — three refs because a session has two sources, staging and per-thread prod targets, plus their composite. ghpr's single ref is correct for its single-source shape; only the namespace is worth changing.)

## 2. `diff` should consume the base: three-way classification — TODO

ghpr built the merge base but `ghpr diff` doesn't read it yet. A two-way diff structurally can't say which side moved — a hunk is equally "GitHub edited this" and "you edited this and `pull` is about to eat it", and those want opposite verbs. With the base, compare each side against it instead and emit one stderr line per changed item:

    DESCRIPTION.md: changed on GitHub — `pull` applies it
    z-123-alice.md: changed locally — `push` sends it
    pr#8356.md: CONFLICT — both sides changed since the last fetch

Two details from the thrds implementation (`slck diff`, `~/c/thrds/thrds/cli.py` `_diff_threads`) worth copying:

- **A local-only change flips the printed diff's direction** (remote → local, "what `push` sends"). The pull-direction rendering would show your own edit as something the remote removes — the exact misreading the classification exists to prevent.
- **local == remote stays silent regardless of the base** — both-changed-to-the-same-content is converged, not a conflict.

## 3. `push` upstream gate — TODO

`push.py`'s ref handling is still advance-only (`set_remote_ref('HEAD')` gated on "was anything held back"); nothing refuses when the remote moved underneath. Sequence: fetch at push-start; if the ref moves, someone (rjpower, say) edited since your last pull — refuse, naming the changed files, with `pull` / `--force` as the outs. ghpr's writes are less destructive than thrds' (no hard deletes), but a PATCH on a comment whose body moved still stomps the newer edit, and the gate is ~free once fetch exists.

Detect per file for the error message; refuse the whole push rather than partially applying (a commit that touched two files means both — see the thrds spec's "Detect per thread; refuse the whole push" section for the reasoning).

## 4. Bootstrap edge to test before 0.3.0 — DONE (fixed differently)

`ensure_remote_ref` initializes the ref to HEAD, assuming local == remote, with a warning. If HEAD has **unpushed local content** at that moment, the first `pull -m rebase` computes `base..HEAD = ∅` (nothing to replay), and the branch lands on the fetched snapshot — the old commits survive as ancestors but their content is reverted in the tree, and the next push propagates the reversion. That is byte-for-byte the failure `d119a07`'s own commit message opens with ("an unpushed local commit survived as an ancestor but its content was reverted — and then pushed"), recreated on the one path where the base is a guess rather than an observation.

thrds sidestepped this structurally (its composite tree overlays HEAD, so local-only content passes through a first pull unharmed, then `push` sends it). For ghpr the cheap fix might be: on bootstrap, *fetch first* and set the ref to the observed snapshot parented on HEAD — the warning becomes unnecessary and the first pull behaves like every later one. Worth a test either way; the `8356-66` live datapoint being waited on exercises fetch+rebase but (probably) not this bootstrap-with-local-delta path.

## Status

**§1 and §4 done** (ghpr, 2026-08-20); §2 and §3 still open.

§1 landed as specified. Verified the premise first: `refs/ghpr/remote` accumulates 0 reflog entries across two `update-ref`s while `refs/remotes/github/remote` accumulates 2, and only the latter answers `@{1}` or appears in `git branch -r`. Migration happens inside `read_remote_ref()`, so any command that reads the base migrates it.

One correction to the framing: "what did GitHub look like before this fetch" was already answerable without the reflog, because every snapshot is committed on top of the previous base, so `git log github/remote` *is* the history of remote states. What the reflog adds is `@{1}` addressing, movement timestamps, and gc protection if that chain is ever broken. Worth doing regardless — the namespace is simply the right one — but the ref was not as history-less as stated.

**§4 was fixed, but not as proposed.** Setting the ref to the fetched snapshot parented on HEAD doesn't close the hole: the snapshot has HEAD as an ancestor, so `base..HEAD` is *still* empty and the next reconcile still lands the branch on remote content. It only appears to work when the remote hasn't moved.

The deeper point is that bootstrap ambiguity is symmetric, and the spec only names one horn:

- assume GitHub is ahead (`base := HEAD`) → unpushed local work is reverted, then pushed;
- assume local is ahead (skip the reconcile) → `push` sends a stale HEAD over a newer remote.

Neither is safe without evidence, so ghpr now refuses to invent one. `resolve_base()` returns `(HEAD, is_bootstrap=True)`, the fetch runs, and `bootstrap_is_ambiguous()` adopts the guess only if the snapshot came back identical to HEAD (i.e. the guess is *confirmed*, the common case for a repo that was in sync). Otherwise it errors with the ref left unset — so the next run still sees an honest bootstrap rather than a base ghpr made up — and names the two commands that constitute an answer: `ghpr pull -m overwrite` (remote wins) or `ghpr push` (local wins). Both are allowed through the check, since each is an explicit statement of which side is authoritative.

Tests in `tests/test_fetch_pull.py`: `TestBootstrap` (local delta not reverted, clean bootstrap adopted, each resolution path, `fetch` alone refuses) and `TestLegacyRefMigration`. 192 passing.

Note on §3 for whenever it's picked up: `pull` already fetches immediately before calling `push`, so a naive fetch-at-push-start would double-fetch on every pull. The gate wants the already-computed snapshot threaded through rather than a second fetch.
