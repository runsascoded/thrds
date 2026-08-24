# Configurable `gh/new` path

## Motivation

Today, `ghpr init` and `ghpr create` both hardcode `gh/new/` as the staging
directory for an unfiled PR/issue draft. This forces a serial workflow: you
can only have one draft at a time, and you have to file (or destroy) it
before starting the next.

In practice, especially when using ghpr to "pre-register" multiple related
issues at once (e.g. several specs / experiment proposals worked on
together), it's natural to want several drafts staged in parallel. Today
that requires either filing too eagerly or sidelining draft text outside
the ghpr-managed flow.

## Proposal

Allow `ghpr init` to accept a custom path/slug for the draft directory,
and have `ghpr create` accept a matching argument to find it. Default
behavior (no args) stays exactly as-is: `gh/new/`.

### CLI surface

```
ghpr init [-r REPO] [-b BASE] [PATH]
ghpr create [-i] [-y] [...] [PATH]
```

`PATH` is interpreted as:
- absolute or relative path → use as-is
- bare slug (no `/`) → resolved as `gh/new-<slug>/` (or `gh/<slug>/` —
  see "Naming convention" below)

Both forms work; the bare-slug form is the ergonomic default.

### Naming convention (resolved)

Went with option **(1) `gh/new-<slug>/`**:
- Preserves the "this isn't filed yet" visual cue.
- Avoids collision with post-file `gh/<number>/` naming.
- Clean one-way state transition on filing: `gh/new-<slug>/` → `gh/<number>/`.

### Shell aliases (resolved)

Adopted the cleaner approach: `ghpr init` prints `GHPR_DIR:<path>` on
stdout (mirroring `ghpr clone`), and the `ghpri` shell function (bash +
fish) parses that marker and `cd`s into the resolved directory. Same
mechanism `ghprc` already uses, so no special-casing for slug parsing
in shell.

## Implementation notes

- `commands/create.py` — added `_resolve_draft_path(path)` helper.
- `init()` accepts optional `path`, uses resolved dir, prints
  `GHPR_DIR:<path>` to stdout for shell-integration.
- `create()` accepts optional `path`; if given, `cd`s into the resolved
  draft dir before running the create flow.
- Existing rename logic at `_finalize_created_item` already keys on
  `parent_dir.name == 'gh'`, which works for `gh/new-<slug>/` without
  changes.
- Existing `gh/new/` users keep working (default `None` arg).
- Tests: `test_create.py` now has `TestResolveDraftPath` (4 unit tests
  for path resolution) and `TestInitSlugMode` (4 tests covering slug
  init, parallel drafts, `GHPR_DIR` marker, collision error).

## Out of scope

- Mass operations across multiple drafts (`ghpr create-all`, etc.) —
  premature; can add if multi-draft usage actually picks up.
- Rename existing drafts after init (e.g. `gh/new-foo/` → `gh/new-bar/`).
  Trivial with `mv`, no need for a CLI verb.
