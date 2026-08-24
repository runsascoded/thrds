# Sync threaded PR review comments

## Implementation status (done)

Implemented. **The on-disk layout changed from the dir-based sketch below to a
flat one** (see "On-disk layout (final)") — gists have no directories, so the
per-PR repo's tree (force-pushed to the mirror gist) must stay flat. Summary:

- `src/ghpr/api.py`: `list_review_comments` (REST, `--paginate`),
  `list_review_threads` (GraphQL), `reply_to_review_comment`,
  `update_review_comment`, `resolve_review_thread`, `unresolve_review_thread`.
- `src/ghpr/reviews.py` (new): per-comment frontmatter file I/O, filename
  parsing/grouping (`scan_local_threads`), `group_threads`, `pull`, `push`,
  `diff`, plus convenience helpers. No PyYAML dependency. Validated against
  [Open-Athena/ops#46] (20 comments → 10 threads, round-tripped a resolve
  toggle + edit + reply via dry-run).
- Wired into `commands/pull.py`, `commands/push.py`, `commands/diff.py`
  (PR-only; skipped for issues and under `--no-comments`). The top-level
  issue-comment glob was tightened `z*.md` → `z[0-9]*.md` so `z-…` review
  files aren't mistaken for issue comments.
- `commands/review.py` (new): the optional convenience group shipped in v1 —
  `ghpr review reply <thread> [-m]`, `ghpr review resolve|unresolve <thread>`.
  `<thread>` accepts a head-comment id or a `PRRT_…` node id.
- Tests: `tests/test_reviews.py` — pure-function unit tests plus
  `pull`/`push`/`diff` orchestration tests that monkeypatch the six `api.py`
  functions (no live `gh` calls).

### On-disk layout (final)

Flat files alongside the description and top-level comments:

```
gh/<num>/
  <repo>#<num>.md                    # description
  z<id>-<author>.md                  # top-level issue comments (unchanged)
  z-3012924106-00-Copilot.md         # review thread head (id 3012924106)
  z-3012924106-01-ryan-williams.md   # reply 1
  z-3012924124-00-Copilot.md         # another thread's head
  z-3012924124-new.md                # draft reply (posted + renamed on push)
```

- `z-<head_id>-<NN>-<author>.md`: `<head_id>` = thread's top comment id, `<NN>`
  = zero-padded sequence (`00` = head, `01`,`02`,… = replies in id order).
  Lexical sort groups a thread and keeps the head first.
- Per-comment frontmatter (`<!-- key: value -->`) carries `author`, `id`,
  `created_at`, `updated_at`, and (replies only) `in_reply_to`. The **head**
  file additionally carries the thread metadata that the dir-based sketch put
  in `.thread.yml`: `path`, `line`, `side`, `start_line`, `start_side`,
  `commit_id`, `original_line`, `thread_node_id`, `resolved`, `is_outdated`.
  Frontmatter is local-only (stripped before pushing the comment body).
- Draft replies: `z-<head_id>-new[-<slug>].md` (body only). On push they're
  posted and renamed to the next `z-<head_id>-<NN>-<author>.md`.

Decisions vs. open questions:

- **Drift detection** uses a baseline under `<git-dir>/ghpr/reviews/<head_id>.json`
  recording the last-pulled `resolved` state. Push refuses a resolve toggle
  only when the remote moved away from that baseline *and* disagrees with the
  local intent; otherwise it applies the local value. Comment-edit drift is not
  separately guarded (matches the existing top-level-comment posture: compare
  local body vs. remote, PATCH if you own it).
- **Gist mirroring** (open question, "Gist mirroring" section): the flat layout
  means review files now mirror to the gist cleanly alongside the description
  and top-level comments, rather than being excluded. They're tracked in the
  per-PR git repo (open question #2 — "keep tracking").
- Outdated threads (null `line`) keep `original_line`; diff display falls back
  to it.

## Context

ghpr today syncs two things between local files and a GitHub PR:

1. **PR description** — `gh/<num>/<repo>#<num>.md`
2. **Top-level issue comments** — `gh/<num>/z<id>-<author>.md`

Review-thread comments (the inline ones attached to a file + line, plus
their replies and resolved state) are not handled. Today, replying to
e.g. a Copilot or human reviewer's inline comment means hand-rolling
`gh api repos/.../pulls/{num}/comments/{id}/replies -f body=...` for
each reply, and `gh api graphql` for `resolveReviewThread` mutations.
That's fine occasionally; on a PR with N inline comments it gets
tedious and noisy in shell history.

This came up concretely on [Open-Athena/ops#46] and [Open-Athena/ops#47],
where Copilot left 7+ inline comments per PR and Jesse left 3 on #46.
Drafting replies in `$EDITOR` and pushing them in a batch (the way the
existing top-level-comment flow works) would have been substantially
nicer than `gh api` one-shots.

[Open-Athena/ops#46]: https://github.com/Open-Athena/ops/pull/46
[Open-Athena/ops#47]: https://github.com/Open-Athena/ops/pull/47

## Goals

- Pull all review-thread comments + their resolved state to local files.
- Edit existing comments locally (your own — GH only allows editing your
  own); push edits back via `PATCH`.
- Reply to existing threads by adding a new local file; push posts a
  threaded reply.
- Mark a thread resolved/unresolved locally; push reconciles via the
  GraphQL `resolveReviewThread` / `unresolveReviewThread` mutation.
- Out of scope for v1: **creating new top-level review-comment threads**
  from scratch (the `path` + `line` + `commit_id` UX is fiddly and
  rarely needed when ghpr is being used by a participant, not the
  initial reviewer — most users would do this through the GitHub UI or
  Copilot/CI tooling, then `ghpr pull` the resulting thread).

## API surface

GitHub REST + GraphQL both have what we need:

| Operation | Endpoint |
|---|---|
| List review comments (with replies inline) | `GET /repos/{o}/{r}/pulls/{n}/comments` |
| Reply to a thread | `POST /repos/{o}/{r}/pulls/{n}/comments/{id}/replies` (body only) |
| Edit a review comment | `PATCH /repos/{o}/{r}/pulls/comments/{id}` |
| Delete a review comment | `DELETE /repos/{o}/{r}/pulls/comments/{id}` |
| List threads + resolved state + node IDs | GraphQL `pullRequest.reviewThreads` |
| Resolve a thread | GraphQL `resolveReviewThread(threadId)` |
| Unresolve | GraphQL `unresolveReviewThread(threadId)` |
| (out of scope, v1) Create new top-level review comment | `POST /repos/{o}/{r}/pulls/{n}/comments` with `path`/`line`/`side`/`commit_id` |

REST returns each comment with `id`, `in_reply_to_id`, `path`, `line`,
`side`, `original_line`, `commit_id`, `user.login`, `body`,
`created_at`, `updated_at`. It does **not** return the GraphQL thread
node ID or the `isResolved` flag — those have to come from a separate
GraphQL query on `pullRequest.reviewThreads`. We need both: REST for
the comment content + reply structure, GraphQL for resolved state +
the `PRRT_…` node ID required by `resolveReviewThread`.

The join key between the two: GraphQL `reviewThreads.comments.nodes[].databaseId`
matches REST `id`. So we fetch both, build a map from REST comment id
→ thread node id, and store the thread node id alongside the thread's
head comment locally.

## On-disk layout

Each review thread becomes one directory under `gh/<num>/reviews/`,
named by the head comment's REST id:

```
gh/<num>/
  reviews/
    3438519490/
      .thread.yml            # path, line, side, commit_id, resolved, thread_node_id
      3438519490-jder.md     # head comment (jder's inline review)
      4000000001-ryan-williams.md   # reply by ryan
      4000000002-jder.md            # reply by jder
```

- One directory per thread → easy to `ls reviews/` and see all threads.
- Each comment is its own file (`<id>-<author>.md`), matching the
  existing top-level comment convention (`z<id>-<author>.md`). The
  `z` prefix is dropped here because we're inside a per-thread dir.
- Files within a thread sort lexically by id (which is also chronological
  for GitHub IDs).
- `.thread.yml` holds metadata that isn't per-comment:

  ```yaml
  path: aws/oa-ci/electrai.py
  line: 117
  side: RIGHT
  commit_id: 1a15232…
  thread_node_id: PRRT_kwDON3bifc6KpC2a
  resolved: false
  # `original_line` retained for outdated-thread display
  original_line: 117
  ```

Each `<id>-<author>.md` file has the same frontmatter as today's
top-level comments (author, created_at, updated_at) plus an
`in_reply_to:` field when the comment is a reply (head comment omits
it). Body is the comment markdown.

### Drafting new replies locally

To reply, drop a file in the thread dir whose name has **no leading
digit** (so we don't collide with real GH IDs) — e.g.
`reply-1.md`, `reply-2.md`. On `ghpr push`:

1. Each such file is POSTed as a reply to the head comment of its thread.
2. On success, the response's `id` is used to rename the file
   (`reply-1.md` → `<new_id>-ryan-williams.md`), and ghpr commits the
   rename so the next pull is a no-op.

(Alternative considered: a single `replies.md` per-thread that gets
split. Rejected — per-file keeps the symmetry with pulled comments and
lets edits/diffs work uniformly.)

### Toggling resolved

Editing `.thread.yml`'s `resolved: false` → `true` (or vice versa)
queues a GraphQL mutation on push. The mutation needs `thread_node_id`,
which we always have because pull populated it.

## Pull semantics

`ghpr pull` extension:

1. After the existing PR body + issue comment sync, fetch
   `GET /pulls/{n}/comments?per_page=100` (paginate).
2. Fetch GraphQL `pullRequest.reviewThreads(first: 100)` for thread
   node IDs + `isResolved` + `comments.nodes[].databaseId`.
3. Group REST comments into threads by walking `in_reply_to_id` chains
   (head = no `in_reply_to_id`).
4. For each thread:
   - Ensure `reviews/<head_id>/` exists.
   - Write `.thread.yml` from head comment's `path`/`line`/`side`/
     `commit_id` + GraphQL `id`/`isResolved`.
   - Write/update each `<id>-<author>.md` from REST data.
5. Commit changes with a message like
   `"Pull from PR: N review thread(s) updated, M comment(s) added"`.

Outdated threads (commit_id ≠ head SHA) are still written, with
`.thread.yml` reflecting `original_line` so a reader can see where the
comment was anchored.

## Push semantics

`ghpr push` extension, run after existing PR body / issue comment push:

For each `reviews/<head_id>/`:

1. Compare local `.thread.yml` to last-pulled state (stored in
   `.git/ghpr/last-pull/<head_id>.yml` or similar). If `resolved`
   changed, queue resolve/unresolve mutation.
2. For each `<id>-<author>.md` whose `id` exists remotely: if body
   changed and author matches the authenticated user, `PATCH` it. Bail
   loudly if a non-authenticated-user file diverged (means someone
   else edited remotely — should pull first).
3. For each file with non-numeric prefix (`reply-*.md`): POST to
   `/comments/{head_id}/replies`, capture new id, rename file, stage
   rename.
4. Fire any queued GraphQL mutations.
5. Commit the rename(s) with a message like
   `"Push to PR: N reply(ies), M resolved"`.

Like the existing push, this should be **safe to re-run** — a re-push
after a successful push is a no-op (all files have real IDs, no
unresolved diffs).

## Conflict / drift handling

Same posture as existing flow: ghpr push refuses to overwrite remote
state it didn't pull. Mechanism: every push verifies the head comment's
`updated_at` matches what we pulled; if remote moved, error and tell
user to pull first. For threads, also verify `isResolved` matches
last-pulled — if someone resolved out-of-band, surface that.

## CLI surface

Stay narrow:

- `ghpr pull` — extended to fetch review threads (no flag needed; just
  do it).
- `ghpr push` — extended to push reply/edit/resolve diffs.
- `ghpr diff` — extended to show review-thread changes (new replies,
  resolve toggles, edits).

Optional convenience (could ship later, not required for v1):

- `ghpr review reply <thread_id_or_head>` — open `$EDITOR` on a new
  `reply-<n>.md` in that thread's dir, pre-stamped with author
  frontmatter. Saves you `cd reviews/<id>/ && $EDITOR reply-1.md`.
- `ghpr review resolve <thread_id>` / `unresolve` — flip the flag in
  `.thread.yml` without hand-editing the YAML.

Both shortcuts wrap pure file edits — no new push logic.

## Gist mirroring

Existing flow mirrors the PR body + top-level comments to a private
gist. Review threads should probably **not** mirror — they're inline
to specific code locations, which loses meaning out of the diff
context, and gists don't render the threading well. Leave them
local-only. (Open question: does anyone use the gist as a primary
view? If yes, revisit.)

## Implementation sketch

New modules / extensions:

- `src/ghpr/api.py`:
  - `list_review_comments(owner, repo, num)` — REST, paginated.
  - `list_review_threads(owner, repo, num)` — GraphQL.
  - `reply_to_review_comment(owner, repo, comment_id, body)` — REST POST.
  - `update_review_comment(owner, repo, comment_id, body)` — REST PATCH.
  - `resolve_review_thread(thread_node_id)` — GraphQL.
  - `unresolve_review_thread(thread_node_id)` — GraphQL.
- `src/ghpr/reviews.py` (new): pulling, parsing, writing, diffing review
  threads. Mirrors the shape of `comments.py`.
- `src/ghpr/commands/pull.py`: call `reviews.pull(...)` after existing
  comment sync.
- `src/ghpr/commands/push.py`: call `reviews.push(...)` after existing
  comment push.
- `src/ghpr/commands/diff.py`: include review-thread diff section.
- Tests: a fixture PR with a couple of threads (one resolved, one
  with replies), exercising round-trip pull → edit → push → pull.

## Out of scope for v1

- **Creating brand-new top-level review comment threads** (anchored to
  a path + line on a specific commit). The on-disk story is harder
  (need a way to author `.thread.yml` without a head comment id;
  collision-proof temp slug for the dir; etc.) and the use case is
  weaker — ghpr is mostly used as a participant, replying to threads
  others started. Could be a v2 feature: `ghpr review new <path>:<line>`
  scaffolds a new thread dir.
- **Hiding/minimizing comments** (GraphQL `minimizeComment` mutation) —
  rare, can stay one-off via `gh api`.
- **Suggested-changes blocks** as a first-class concept — they round-trip
  fine as markdown today, no special handling needed.
- **Multi-line review comments** (`start_line` / `start_side`) — store
  the fields in `.thread.yml`, but don't add CLI affordances for
  authoring multi-line threads in v1.

## Open questions

1. Does it bother anyone that thread dirs live under
   `gh/<num>/reviews/` while top-level comments stay at
   `gh/<num>/z<id>-…`? Could move the latter into `gh/<num>/comments/`
   for symmetry, but that's a separate breaking change with its own
   migration story. Keep separate for now.
2. Should pulled review-thread files be checked into the per-PR git
   repo (current behavior for top-level comments), or kept untracked?
   Tracking gives history-of-the-review for free, at the cost of
   extra commits. Lean: keep tracking, matches existing flow.
3. Resolve-state drift: if someone resolves on the web while you have
   `resolved: false` locally, how loud should the error be?
   Probably: refuse to push, dump a diff, tell user to pull.
