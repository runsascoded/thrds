# Migrate draft layout to `gh/drafts/<slug>/`

## Context

`specs/done/configurable-new-path.md` shipped (`ef3910a`) with this
slug→path resolution:

- no arg → `gh/new/`
- bare slug `foo` → `gh/new-foo/`
- path with `/` → used as-is

After using this in practice (drafting several issues at once for the
`tomat` repo), the `gh/new-<slug>/` flat layout has a few rough edges:

1. Drafts and filed dirs (`gh/<number>/`) are siblings, so `ls gh/`
   shows a mix of `42/`, `43/`, `new/`, `new-foo/`, `new-bar/`.
   Visually noisy.
2. `gh/drafts/` (or `gh/new/` as a single slot) is what feels natural
   when you just want "all my unfiled drafts".
3. `new-<slug>` doesn't sort or filter cleanly — slugs starting with
   `new-` would technically collide.

## Proposal

Resolve bare slugs into `gh/drafts/<slug>/`:

- no arg → `gh/new/` (unchanged, back-compat)
- bare slug `foo` → `gh/drafts/foo/`
- path with `/` → used as-is

On filing (`ghpr create`), move the dir to `gh/<number>/` as today.
Listing `gh/drafts/` then naturally shows only in-flight drafts, and
`gh/` itself is dominated by numbered filed dirs.

## Implementation

`src/ghpr/commands/create.py:_resolve_draft_path()`:

```python
def _resolve_draft_path(arg: str | None) -> Path:
    if arg is None:
        return Path('gh/new')
    if '/' in arg:
        return Path(arg)
    return Path('gh/drafts') / arg
```

Plus one extra change required by the new layout: the rename logic in
`_finalize_created_item` previously checked `parent.name == 'gh'` to
decide whether to rename `gh/new/` → `gh/<number>/` on filing. With
drafts now at `gh/drafts/<slug>/`, the parent is `drafts`, not `gh`.
Generalized the check to walk up parents and find the `gh` ancestor,
then rename to `<gh>/<number>/`. This handles all three layouts:
- `gh/new/` → `gh/<number>/`
- `gh/drafts/<slug>/` → `gh/<number>/`
- `gh/new-<slug>/` (legacy) → `gh/<number>/`

Tests were updated for the slug-mode expected paths (was `gh/new-foo`,
becomes `gh/drafts/foo`).

`gh/new/` (no-slug default) stays as-is for back-compat. Could
deprecate in a follow-up if desired — e.g. emit a hint suggesting
`ghpr init <slug>` when called with no args, encouraging users into
the new layout.

## Migration for existing users

Anyone who already has `gh/new-<slug>/` dirs from the previous
release should either:
- File them (`ghpr create -i`) — they become `gh/<number>/`, agnostic
  to layout.
- Or `mv gh/new-<slug> gh/drafts/<slug>` — manual one-time migration;
  no code change needed since drafts are self-contained git repos.

Document in the release notes; no automatic migration code.

## Out of scope

- Removing the legacy `gh/new/` no-arg default — keep for back-compat;
  revisit later if usage shows people prefer always-explicit slugs.
- Auto-archiving filed drafts under `gh/filed/<number>/` — could go
  further on the dir-organization theme but not a blocker.
