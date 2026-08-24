# Auto-init nested-tracking dir on `ghpr create`

## Context

`ghpr create` does *most* of what `ghpr clone <N>` does after filing: rename `DESCRIPTION.md` → `{repo}#{N}.md`, commit, push to gist remote, rename dir `gh/drafts/<slug>/` → `gh/{N}/`. The only thing it skips is `git init` for the nested repo. That works as long as the parent repo's `.gitignore` doesn't exclude `gh/`.

When the parent repo *does* ignore `gh/` (which is the recommended setup — drafts and tracked-issue dirs shouldn't be in the parent repo's history), `_finalize_created_item` breaks:

```text
Created issue #5753: https://github.com/marin-community/marin/issues/5753
Issue info stored in git config
Renamed DESCRIPTION.md to marin#5753.md
fatal: pathspec 'DESCRIPTION.md' did not match any files
Error creating issue: Command '['git', 'rm', 'DESCRIPTION.md']' returned non-zero exit status 128.
```

The issue IS filed on GitHub — the failure happens during the local-tracking cleanup. The user's recovery dance is:

```bash
# delete the partially-filed-but-not-properly-tracked draft dir
rm -rf gh/drafts/<slug>/

# re-clone the now-existing issue, which sets up nested git tracking
ghpr clone <N>
```

This is silly because:

1. Two CLIs and a manual `rm -rf` for an operation that should be `ghpr create` followed by nothing.
2. The information needed to do the right thing (the new issue number, the dir to rename, the gist URL) is all already in scope at the failure site.
3. The recovery dance loses any draft-only history the user accumulated under `gh/drafts/<slug>/.git` (if they were already running nested-tracking by hand) — but actually most users won't have set that up for drafts since they didn't know they needed to.

## Proposal

On `ghpr create`, after the GitHub issue/PR is filed but before the rename/commit steps, detect whether the current dir is inside a nested git repo (`git rev-parse --show-toplevel` returns the current dir or a descendant, not an ancestor that owns the parent project). If not, run `git init -q` first, mirroring what `ghpr clone` does at line `clone.py:127`.

Then the existing `git add` / `git rm` / `git commit` operations target the nested repo and succeed regardless of the parent's `.gitignore`.

Net behavior:

| parent has `gh/` ignored | dir already nested-tracked | today | after this change |
|---|---|---|---|
| no | n/a | works | works |
| yes | yes | works | works |
| yes | no (typical) | **fails** | **works** (auto-init) |

## Implementation

`src/ghpr/commands/create.py:_finalize_created_item`, after writing the new file and before the first `git add`:

```python
def _finalize_created_item(owner, repo, number, url, item_type):
    ...
    write_description_with_link_ref(new_file, owner, repo, number, title, body or '', url)

    # Remove old file if different name
    if old_file != new_file:
        old_file.unlink()
        err(f"Renamed DESCRIPTION.md to {new_filename}")

    # NEW: ensure we're inside a nested git repo so subsequent git ops don't
    # collide with the parent project's `.gitignore` (e.g. when `gh/` is
    # gitignored at the project root).
    _ensure_nested_git_repo(owner, repo, number, url, item_type)

    # Git operations
    proc.run('git', 'add', new_filename, log=None)
    ...
```

`_ensure_nested_git_repo`:

```python
def _ensure_nested_git_repo(
    owner: str, repo: str, number: str, url: str, item_type: str,
) -> None:
    """Run `git init` if cwd isn't already the toplevel of its own git repo.

    Mirrors the setup `ghpr clone` does at clone.py:127, so the local-tracking
    behavior of a "create-then-finalize" matches a "clone" exactly.
    """
    try:
        toplevel = proc.run_out('git', 'rev-parse', '--show-toplevel', log=None).strip()
    except subprocess.CalledProcessError:
        toplevel = None
    cwd = str(Path.cwd().resolve())
    if toplevel != cwd:
        proc.run('git', 'init', '-q', log=None)
        # Store metadata in git config, same as clone.py:143-147
        proc.run('git', 'config', 'pr.owner', owner, log=None)
        proc.run('git', 'config', 'pr.repo', repo, log=None)
        proc.run('git', 'config', 'pr.number', str(number), log=None)
        proc.run('git', 'config', 'pr.url', url, log=None)
        proc.run('git', 'config', 'pr.type', item_type, log=None)
        err(f"Initialized nested git repo (parent had {cwd!r} unignored or gh/ ignored)")
```

Two small follow-ups:

1. The `git rm 'DESCRIPTION.md'` needs to handle both cases: tracked
   (existing nested repo from `ghpr init`) and untracked (fresh nested
   repo we just initialized). The spec originally proposed dropping
   `git rm` entirely, but that leaves DESCRIPTION.md as tracked-but-
   missing in the index when init had committed it. Instead, used
   `git rm -q --ignore-unmatch DESCRIPTION.md` (removes from index and
   disk if tracked; silently no-ops if not), followed by an unlink
   fallback for the untracked-and-still-on-disk case.

2. The gist-remote push at `create.py:200` (`git push gist-remote main`) needs the nested repo's `main` branch to actually exist with a commit. The `git commit -m ...` at line 178 covers that, so order is fine.

## Alternatives considered

- **Always create the nested git repo at draft-creation time** (when user first writes `gh/drafts/<slug>/DESCRIPTION.md`). Cleaner state model, but requires a new CLI (`ghpr draft <slug>`?) and a behavior change for existing draft workflows. Out of scope for this change.

- **Detect `.gitignore` rule for `gh/` and skip git ops entirely**. Simpler but loses the local-tracking + gist-sync benefits that nested-repo enables. Not worth it — auto-init is just as easy.

- **Add a `--init-nested` flag**. Punts the decision onto the user. Bad ergonomics.

## Test plan (implemented)

`tests/test_create.py` adds:

1. `TestEnsureNestedGitRepo` (3 unit tests) — exercises
   `_ensure_nested_git_repo` directly with real git:
   - Inits a nested repo when cwd is owned by a parent repo
   - No-ops when cwd is already its own toplevel (preserves config)
   - Inits when there's no git repo at all

2. `TestCreateWithGitignoredGh` (1 integration test) — exercises the
   user-visible failure mode end-to-end:
   - Parent repo with `gh/` in `.gitignore`
   - Manually-created `gh/drafts/test-issue/DESCRIPTION.md` (no
     `ghpr init` was run)
   - Mocks the `gh issue create` call to return a fake issue URL
   - Runs `create_new_issue` and asserts the dir is renamed to
     `gh/42/`, the new file is committed to a nested git repo, and
     no exception is raised.

## Related

- `specs/done/drafts-subdir.md` — the layout this builds on.
- `specs/done/configurable-new-path.md` — the slug→path resolver.
- `src/ghpr/commands/clone.py:127` — the `git init` that `create` is missing.
