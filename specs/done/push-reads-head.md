# `ghpr push`: read from HEAD, not WT (Contract A)

## Status

**Done.** Implemented in `src/ghpr/reviews.py`, `src/ghpr/commands/push.py`, `tests/test_reviews.py`; 164 tests pass. The reply-body edit gap flagged in [Outstanding](#outstanding) was closed before landing: the comment-edit loop now skips WT-dirty synced files with the same guard as the resolve path (`test_push_skips_edit_when_synced_file_dirty` pins it). Shipping in `ghpr-py` 0.2.0.

## Motivation

The stated `ghpr` contract is "user updates WT → user commits → push syncs the committed state to GitHub + gist." In practice the codebase had drifted to three different policies across paths:

| Path | Read source on push | UCs handling |
|---|---|---|
| Top-level description (`{repo}#{N}.md` / `DESCRIPTION.md`) | HEAD | auto-committed via `sync_to_gist` (existing-gist branch, `push.py:514-516`) — Contract B |
| Top-level comments + drafts (`<NN>.md`, `new*.md`) | HEAD (`git show HEAD:...`) | strict — UCs ignored — Contract A |
| Review-thread head files, synced replies, drafts (`z-*.md`) | WT (`path.read_text()`) | inconsistent: drafts get a post-hoc rename-commit; resolve-flips and reply edits are read from WT and never committed → silent drift between HEAD and gist |

The review-thread WIP commit (`d00cc40`) was the outlier. The three immediate consequences in the wild:

1. **The drift we found via `Open-Athena/pulumi#3`**: `ghpr review resolve` writes `resolved: true` to the head-comment file's frontmatter. On push, that file was read from WT to drive the GraphQL mutation; but the WT mutation itself was never staged or committed (`reviews.py:471-480` only commits when there are draft renames). After a successful push: GitHub and the baseline both reflect `resolved: true`, but `HEAD` still says `resolved: false`. Re-pulling didn't help because pull also writes WT and the baseline matches GitHub. The user is left with a permanently dirty WT.

2. **A latent symmetric bug in reply-body edits**: same shape — `$EDITOR`-edited reply files were read from WT, `_patch_body` was called with WT content, but the WT modification was never committed. Hadn't bitten anyone yet because the steady-state invariant `HEAD body == remote body` happened to hold, but the design was vulnerable.

3. **Real data-loss potential discovered during this work** (see [Incident](#incident-during-development)): if a session sets up the Contract-A read (`HEAD` says `false`) without the WT-dirty safety, and the remote already says `true` (from an out-of-band resolve), push will *actively unresolve* on GitHub.

## What was done in this WT

### 1. `reviews.py`: push reads HEAD for all three sources

- New helper `parse_comment_file_at_head(filename) -> tuple[dict, str] | None` runs `git show HEAD:<filename>` and parses frontmatter+body the same way the disk reader does. Returns `None` if the file isn't tracked in HEAD.
- New helper `_wt_dirty(filename) -> bool`: thin wrapper over `git status --porcelain -- <filename>` that returns `True` iff the file is tracked AND modified in WT.
- `push_reviews()` (renamed `push()` in the module — same callable) now:
  - Reads `head_fields` via `parse_comment_file_at_head(hp.name)` instead of `parse_comment_file(hp)`.
  - Reads synced-reply `fields, body` the same way.
  - Reads draft bodies via `proc.text('git', 'show', f'HEAD:{draft.name}', err_ok=True, log=None)`. Drafts that exist in WT but not in HEAD are silently skipped — the pre-push warning surfaces them separately.
- The post-loop commit (`reviews.py:471-480` original) is untouched in spirit but now only ever stages renames (no resolve/edit modifications get touched because none of those paths write to disk anymore).

### 2. `push.py:514-516`: removed description WT auto-commit

In `sync_to_gist`'s existing-gist branch, the block that ran `git add local_filename` + commit-if-staged is gone. The PR description sent to GitHub via API was already read from HEAD (`read_description_from_git('HEAD')` at the top of push), so the auto-commit was actually creating real drift: GitHub got HEAD content, but the gist got WT content (via the freshly-created commit).

The new-gist branch's auto-commit at `push.py:604-609` is left alone — that one is legitimate bootstrap (fresh content was just fetched from the API and written to local for first-push setup; committing it is benign and matches the "rename-only commit" pattern @ryan-williams endorsed during review).

### 3. Pre-push warning

New `_warn_uncommitted_ghpr_files()` runs at the top of `push()` in `commands/push.py`. It walks `git status --porcelain` and reports any modified ghpr-managed file (description, `<NN>.md`, `new*.md`, `z-*.md`). Output:

```
⚠ 2 file(s) with uncommitted changes will be ignored (push syncs HEAD; commit + re-run to include):
    z-3456586766-00-Copilot.md
    z-3456586782-00-Copilot.md
```

Glob patterns are kept in `_GHPR_FILE_GLOBS` for easy extension.

### 4. Per-thread safety: skip resolve when head file is WT-dirty

This is the key safety that prevents the destructive-unresolve case in [Incident](#incident-during-development).

```python
head_wt_dirty = hp is not None and _wt_dirty(hp.name)

# 1. Resolve / unresolve
if node_id and not head_wt_dirty:
    ...
```

The baseline write at the end of the loop is also gated on `not head_wt_dirty`, so we don't mark a thread as "synced" when we deliberately skipped it.

### 5. Tests

- `_commit(path, *paths, msg='wip')` helper added near `_git_init`. Per @ryan-williams's preference, takes explicit paths instead of `git add -A`.
- `TestPushOrchestration._seed_thread` now commits the seeded files (representing what `pull` would do).
- `test_push_edits_own_comment` and `test_push_skips_others_comment_without_force` commit the WT edit before calling push (matching the new "commit before push" contract).
- `test_push_posts_reply_and_renames` commits the draft before push (matching the "selectively publish drafts by committing" property @ryan-williams endorsed).
- New: `test_push_skips_resolve_when_head_file_dirty` asserts the per-thread safety. Critical regression guard — without this, the [Incident](#incident-during-development) failure mode comes back.

## Outstanding

**Closed (reply-body edit path).** The `# 2. Comment edits` loop now skips WT-dirty synced files with the same `_wt_dirty(path.name)` guard used for resolve, so a `$EDITOR` edit that wasn't committed no longer causes push to send stale HEAD content over a remote edit made out of band:

```python
for _seq, author, path in g['synced']:
    if _wt_dirty(path.name):
        continue   # user intent is in WT but not committed; don't act on stale HEAD
    parsed = parse_comment_file_at_head(path.name)
    ...
```

`test_push_skips_edit_when_synced_file_dirty` pins the behavior alongside `test_push_skips_resolve_when_head_file_dirty`. The dirty files are already surfaced to the user by the pre-push warning (`_warn_uncommitted_ghpr_files`), which lists every dirty ghpr-managed file by name, so no extra per-thread messaging was added. We kept the two inline `_wt_dirty()` checks (resolve + edits) rather than extracting a `_should_skip_thread_mutation()` helper — with only two call sites the abstraction wasn't worth it, and the inline checks read symmetrically.

**Still open (description-edit path).** The top-level description path doesn't have explicit skip semantics. Today's behavior: user edits `{repo}#{N}.md` in WT without committing → push reads HEAD content for the API call → patches GitHub with the OLD content. Same failure mode as the reply-edit case if HEAD is stale relative to a remote that someone else updated. The pre-push warning does surface the dirty file, but there's no hard skip. Possibly out of scope for this spec; flagging for awareness.

## Incident during development

While verifying the fix against `~/c/pulumi-v1/gh/3`, the WT had `resolved: true` for two head files (the leftover of the pre-fix `ghpr review resolve` modifications that the old code had failed to commit). After the Contract A refactor, push:

1. Read `resolved: false` from HEAD (the stale committed state).
2. Saw GitHub already reported `resolved: true`.
3. Saw the baseline was `{"resolved": true}` (matched GitHub — no drift).
4. Concluded "local says false, remote says true, no drift → user wants to unresolve" and applied `unresolveReviewThread` to both threads on GitHub.

Recovery: direct GraphQL `resolveReviewThread` mutations against both `thread_node_id`s. The `_wt_dirty` safety in step 4 above prevents this class of error going forward, and there's a test pinning it. But the incident is the reason I want @ryan-williams to weigh in on the generalization in [Outstanding](#outstanding) before landing — the resolve path is now safe by an explicit `if`, but the symmetric edit path is safe only by accident, and a future change could easily break that accident.

## Validation

Beyond the test suite, the work was validated end-to-end against `Open-Athena/pulumi#3` from `~/c/pulumi-v1/gh/3/`:

1. Started with 2 head files WT-dirty (`resolved: false` in HEAD, `resolved: true` in WT) and 2 corresponding threads resolved on GitHub.
2. Ran `ghpr push` → pre-push warning fired; resolve mutations were skipped for both threads (verified via `grep -E "Resolved|Unresolved" push.log` returning empty + GitHub state unchanged).
3. Committed the WT changes: `git commit -m "Mark threads X, Y resolved"`.
4. Ran `ghpr push` twice in a row. First run synced baseline (still no GitHub mutations needed). Second run had zero output for review threads — fully idempotent.
5. `ghpr diff` showed zero pending changes; WT was clean.

## Commit shape (as landed)

One commit, "Push: read HEAD, not WT (Contract A)" — `reviews.py`, `commands/push.py`, `tests/test_reviews.py`. Covers the HEAD-read refactor, the description auto-commit removal, both `_wt_dirty` mutation gates (resolve + edits), and both regression tests. The rename-only post-push commit and the existing-gist auto-commit removal are part of the same coherent change, so splitting would be artificial.
