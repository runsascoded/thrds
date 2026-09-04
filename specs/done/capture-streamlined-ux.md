# Streamlined `thrds capture` UX

From the jc-taxes session (via `/read`, 2026-09-03): `thrds capture` worked for the `$c/voice` example gist, but took too many steps and produced a noisy artifact. The ask (user's sketch):

```bash
echo "$claude_draft" | thrds capture init
echo "$final_msg" | thrds capture update [dir from last cmd]
```

Two simple commands → a secret, 1-file, 2-revision gist, no platform target. Their session also diagnosed two state smells to fix along the way:

- `thrds.yml` serializes the **entire Slack-oriented `SessionState`** (empty `staging_threads`, `staging_chrome`, `remotes`, …) even for capture sessions, where the only load-bearing fields are `session_id` / `doc_path` / `gist_id` / `gist_remote` / `platform`.
- The capture **gist** carries `thrds.yml` at all, though its stated reason to be mirrored (multi-machine slug→`thread_ts` sync) doesn't exist for capture; the gist should be purely the doc and its revisions.

## Design

### 1. `capture init` reads stdin

`DOC_PATH` becomes optional. When omitted (or `-`), the doc content comes from stdin:

```bash
dir=$(echo "$claude_draft" | thrds capture init)
```

- Slug: `-s/--slug` wins; else `slugify()` of the content's first line (same helper the Slack adopt flow uses — strips md emphasis/heading markers); else a usage error asking for `-s`.
- The **session dir prints to stdout** (absolute path) — primary, pipeable output; all existing hints stay on stderr. Printed in both stdin and `DOC_PATH` modes, and on resume.
- stdin mode with a TTY on stdin → usage error (pass `DOC_PATH` or pipe content).

### 2. New verb: `capture update [SESSION_DIR]`

Replace the doc with stdin, commit, push → one gist revision:

```bash
echo "$final_msg" | thrds capture update "$dir"
```

- `SESSION_DIR` defaults to the CWD (which must be a capture session — platform-guarded like every verb).
- Content identical to the current doc → no commit, no push, `(no changes — nothing to push)` on stderr, exit 0 (idempotent).
- TTY on stdin → usage error pointing at `capture push` (which remains the "I edited the file in place" verb).

### 3. Slim `thrds.yml`

`SessionState.save()` prunes fields whose value equals their dataclass default, for **all** platforms — `load()` restores defaults, so the round trip is identical state. `session_id`, `doc_path`, and `platform` are always serialized (identity fields stay explicit). The documented `channel_prefix: ''` vs `null` distinction survives: `''` differs from the default (`None`) so it's kept.

A capture session's `thrds.yml` drops from ~20 lines of Slack cruft to ~5.

### 4. Capture gists carry only the doc

For `platform == 'capture'`:

- `init` appends `/thrds.yml` + `/thrds.json` to the session repo's `.git/info/exclude` — state is a local-only file, never in git history, therefore never in the gist.
- The post-gist-creation state commit (`thrds: init <slug> (gist <id>)`) is skipped — the gist's seed commit (just `<slug>.md`) is the shared history; `gist_id` is recoverable from the `g` remote URL if `thrds.yml` is lost.
- `push` / `update` stage only the doc. **Legacy** capture sessions that already track `thrds.yml` keep staging it (checked via `is_tracked`), so their history stays consistent.

## Non-goals

- No changes to slack/discord/bsky init UX (stdout dir printing is capture-only for now).
- No migration of existing capture sessions' tracked `thrds.yml` (harmless; removable by hand).

## Acceptance

```bash
dir=$(echo 'draft v1' | thrds capture init)      # secret gist, rev 1, dir on stdout
echo 'final v2' | thrds capture update "$dir"    # rev 2
echo 'final v2' | thrds capture update "$dir"    # no-op, exit 0
```

Gist contains exactly one file (`<slug>.md`) with two revisions; local `thrds.yml` is ~5 lines and untracked.

## Implemented (2026-09-04)

All four design points landed as specced, with these notes:

- Slug derivation reuses `threadfile.slugify` (first line, md emphasis stripped, ≤6 words) — `# My Draft` and `**My Draft**` both yield `my-draft`.
- The slim serialization is generic — `SessionState.save()` prunes default-equal fields for **every** platform (a fresh slack session's `thrds.yml` is also just the identity trio now). Comparison is against the pruned default, and required fields without defaults are never pruned.
- `capture init` prints the session dir to stdout in all modes (path mode, stdin mode, resume); hints stay on stderr.
- `_capture_state_paths` centralizes the "stage the doc; stage `thrds.yml` only if a legacy session already tracks it" rule, shared by `push` and `update`.
- The state exclusion is written to `.git/info/exclude` (not a tracked `.gitignore`) so the exclusion itself never reaches the gist.
- `capture update` with identical content exits 0 with `(no changes — nothing to push)` and no commit; `--no-gist` sessions print the existing `(no gist configured — commit only)` note.
