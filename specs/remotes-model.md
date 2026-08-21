# Named remotes: the unifying abstraction under thrds and ghpr

Ryan's framing (2026-08-21), correcting this session's claim that thrds and ghpr "genuinely differ where it counts" (staging/prod lifecycle vs. a single canonical GitHub item):

> "staging" and "prod" Slack channels are each a "remote" that should be fetch/pull/push-able, just like a GH "item". For dscrd we've discussed using our own server as a "staging" "remote"; the semantics and naming around specific "remotes" should be flexible, just like with real Git remotes. In principle a GH item could have a staging "remote": a throwaway issue or PR in an ephemeral private repo created for the purpose. […] they should get hooked in to Git vocabulary at a low level ("remotes"), and then offer Git-like semantics on top of that.

He's right. What I called a model difference is a **remote-topology difference**: thrds sessions conventionally have two pseudo-remotes (staging PC, per-thread prod targets); ghpr items have one (the GitHub item). Nothing in git privileges `origin` — conventions do — and the same holds here. Everything thrds treats as lifecycle re-derives from two git-native ideas:

- **A remote** is anywhere content can be fetched from / pushed to: a Slack channel, a Discord server, a GH issue/PR, a gist. Some are provisionable (a PEC; an ephemeral private repo for GH staging) — that's `git remote add` plus a creation hook.
- **Per-item upstream** is which remote is canonical for a thread right now. `draft` ⇒ upstream = staging; `posted` ⇒ upstream = prod. `promote` = *first push of a thread to its prod remote, plus flipping its upstream* — `git push -u prod <thread>`, morally. `pull`'s prod-over-staging precedence is just "pull from upstream". `ThreadEntry.state` is a projection of the upstream pointer, not an extra concept.

Checked against the features that looked lifecycle-specific — each is a per-remote property, not a model exception:

| feature | in remotes vocabulary |
| --- | --- |
| staging-only chrome footer | a rendering option of the staging remote |
| promote's append-only-in-shared-threads (`only_ids`) | write-scoping property of pushing into a remote you don't own |
| PEC creation on first push | provisionable remote (`remote add` + create hook) |
| `posted_msg_ts` whitelist | per-remote tracking of which messages are ours |
| frozen staged copy after promote | the non-upstream remote is no longer converged, only observed |
| cross-thread `#slug` refs | resolved per-remote (permalinks differ per channel) |

## What's already aligned

`refs/remotes/<platform>/{staging,prod}` (`tracking.py`) is this structure in embryo — one tracking namespace per pseudo-remote. The composite `slack/remote` is, in remotes terms, the **upstream projection**: each thread's content as its upstream remote reports it, materialized as one tree because the session reconciles as one branch. It's an implementation detail of "pull from upstream", not a third remote; possibly worth renaming (`slack/upstream`?) once the vocabulary lands, though `remote` is currently converged with ghpr.

## What this implies (roadmap, not this week)

1. **Remotes become named config, not hardcoded roles.** A `remotes` section (in `thrds.json`, or literally `git config`-shaped) mapping name → `{platform, address, options}`; `staging`/`prod` become the *default* topology `slck init` writes, not the only one. Discord's own-server staging and multi-prod-target sessions fall out for free.
2. **Verbs take an optional remote**, defaulting sensibly: `fetch` (all), `pull` (upstream), `push [<remote>]` (staging today; `promote` becomes sugar for `push -u prod <thread>` + the safety plan).
3. **ghpr converges by adopting the same layer**: its item is one remote, the gist a second; a staging remote (throwaway item in an ephemeral private repo) becomes *possible* rather than architectural. This dissolves the strongest argument against eventual unification — the remaining differences (content layout, comment files) are platform adapters, exactly like `slack.py` vs `discord.py`.

## Push reads HEAD, not the working tree (settled)

The second corrected claim: I called ghpr's Contract A (push from HEAD) vs thrds' push-from-WT "a real behavioral fork". Asked whether there's a good reason for the difference — there isn't. Git pushes commits; HEAD is correct. thrds' WT-push is an accident of ordering: `push` writes WT content to Slack *then* auto-commits, so the commit records what was pushed after the fact. The fix is to reorder, not to drop the auto-commit UX: **`push` auto-commits first, then pushes HEAD.** Same effective behavior when the WT is dirty (it all gets committed anyway, as today), but the pushed state is exactly a commit — so the post-write verification and ref advance (fetch-refs step 4) refer to a commit, pushes become transactional, and the HEAD-vs-WT fork with ghpr disappears. The remaining difference — thrds auto-commits, ghpr requires a manual commit — is a per-tool UX choice sitting *on top* of identical push semantics, which is where differences belong.

Lands with fetch-refs step 4 (the push gate), which touches the same code.

## Audit: does the fetch-refs work (steps 1–5) fight this vision? (2026-08-21)

Structurally no — per-remote tracking refs, per-remote write gates, push-from-HEAD are this model's foundation. Four frictions, two of them real:

1. **`slack/remote` is misnamed.** The composite isn't a remote; it's the *upstream projection* — each thread's content as its per-thread upstream reports it, materialized as one tree because the session reconciles as one branch. That's also why it (correctly, but confusingly for a "remote") carries HEAD's passthrough files. `slack/upstream` says what it is. Cheap to rename now (refs are local; migrate on read, as ghpr did for its namespace move) — but "remote" is the name currently converged with ghpr, so the rename should be proposed to both sides rather than done unilaterally.
2. **`slack/prod` conflates N remotes into one ref.** Targets are per-thread; a session can promote different threads to different channels, and "prod" flattens what the model says are distinct remotes into one tree. The file-at-a-time update in `_update_prod_ref` already tacitly admits this. Fine at current scale; a named-remotes implementation would give each target its own `refs/remotes/<name>/` and derive today's behavior as the one-prod-remote special case.
3. **Roles are hardcoded.** `STAGING`/`PROD` constants, `push`→staging, `promote`→prod. Compatible as the default topology `init` writes, which is what the model asks — but `_gate_push`/`_verify_push` bake the staging role in, so the "verbs take a remote" step is a refactor, not a parameter.
4. **`fetch` is all-or-nothing.** Justified originally by composite coherence, but the composite can rebuild from stored refs without API reads (the promote verify already does exactly this), so per-remote fetch becomes legitimate whenever named remotes land. The constraint is an implementation convenience, not a semantic one.

Bootstrap-outs message wording (`slck push` = local wins) is push/staging-scoped and would generalize to "push/promote to the disagreeing remote" — cosmetic.

## Step 1 landed: the resolution layer + per-remote fetch (2026-08-21)

`thrds/remotes.py` is now the seam between verbs and roles: `Remote(name, role, channel)` with `resolve(state)` deriving the default topology (staging from `staging_channel`; prod as the per-thread-targets role) in composite-merge order — prod last, which states the "prod is canonical for a posted thread" rule exactly once. `observe(remote, …)` is the one place that knows how a role is read, replacing four duplicated render-observed blocks in `cli.py`.

- **Friction 3 (hardcoded roles) resolved at the helper layer.** `_gate_push`/`_verify_push`/`_gate_promote`/`_verify_promote`/`_update_remote_file` all take a `Remote`; only the *verbs* still choose one (`push` → staging, `promote` → prod), which is the model's own "default topology" claim. Their error messages interpolate `remote.name`, so today's output is byte-identical.
- **Friction 4 (all-or-nothing fetch) resolved.** `slck fetch [staging|prod]...` reads only the named remotes; a skipped remote keeps its ref un-advanced and feeds the composite from its stored tree (`_merged_observations` — the same no-API rebuild the promote verify already did). Guard: a skipped remote that *has threads but no ref yet* is refused, because with nothing stored its threads would be misrecorded as deleted in the composite.
- **`fetch remote` is refused by name** with a pointer that the composite is derived, refreshed on every fetch.

**Config section deferred, deliberately.** A `remotes:` section in `thrds.json` that names remotes the readers can't honor would be config that lies: a second staging-role remote needs its own slug → ts map (per-remote thread pointers) and channel-parameterized pulls in `slack.py`. That data-model work is the real prerequisite for both the config section and `push [<remote>]`/`pull [<remote>]` args, so those land together, after it.
