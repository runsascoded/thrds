# Spec: multi-thread posts, a markdown format, and draft/sent capture

**Status:** in-progress. Motivated by a 2026-07-31 Trainium status update to an AWS PM, which took five drafts plus a structural rewrite and ended up as **four top-level Slack messages, each with its own reply thread, cross-linked**. Nothing in `thrds` expressed that shape, and the whole draft→sent capture loop was done by hand from screenshots.

## Implementation status (2026-08-05)

Landed:
- **Phase A** — `.md` format + `Doc` model + parser/serializer (`thrds/doc.py`, `thrds/md.py`). Round-trip guaranteed; foreign replies (`+++ @author`) preserved on parse and never posted on push.
- **Phase B** — session state (`.thrds/state.json`, `thrds/state.py`) and `SlackClient.sync_doc_{staging,prod}` with lazy PC creation, terraform-on-staging, additive-on-prod, and thrds-metadata stamping on every posted message.
- **Phase C** — `SlackClient.pull_doc_{staging,prod}` (rebuild a `Doc` from a channel via `conversations.replies`; foreign authors resolved via cached `users.info`; threads returned in OP-ts order) + `md.diff_docs` (canonical-serialize both sides, `difflib.unified_diff`). Closes the parse → push → pull → diff → parse round-trip.
- **Phase D.1** — CLI (`thrds/cli.py`, click-based) with `init`, `push [--prod] [--keep-staging] [--dry-run] [--channel]`, `pull [--prod] [--channel] [--write]`, `diff [--prod] [--channel]`, `archive`. `thrds` entry point installed via pyproject `[project.scripts]`. Depends on `click>=8.0` (breaks the zero-dep stance; deliberate tradeoff for CLI ergonomics).
- **Phase D.2** — gist mirror (`thrds/mirror.py` + reworked `thrds init`). Sessions now live in ghpr-style dedicated dirs (`<git-root-or-cwd>/thrds/<slug>/`) with nested `.git/` (parent-project git sees the dir as an opaque untracked entry — git doesn't recurse into dirs containing `.git/`). `thrds init` creates the subdir, `git init -q -b main`s it, copies (or creates empty) the doc, writes `.thrds/state.json`, makes an initial commit, and (default) shells out to `gh gist create --secret` to create a secret gist + adds it as the `g` remote. State-mutating verbs (`push`, `pull --write`, `archive`) auto-commit their changes and push to `g`. `--no-gist` opts out (local-only git). `staging_archived` field added to state for archive idempotence.
- **Phase E** — cross-thread `[text](#slug)` → permalink resolution (`thrds/refs.py`). Two-phase pattern (mirrors `linked.py`): phase-2 substitutes every `(#slug)` with a fixed-length placeholder URL (180 chars, safe upper bound on real Slack permalinks) and runs the existing sync flow; phase-3 fetches `chat.getPermalink` per referenced OP, rebuilds the ref-containing messages with real URLs, and re-runs `_sync_doc_thread` per affected thread — `core.sync`'s diff detects the changed messages and edits only those. Wired into both `sync_doc_staging` and `sync_doc_prod`; dry-run skips phase 3 (no ts's to link to). Validates dangling refs upfront (fails before any API call). Length-checks messages post-substitution.

Divergences from the design body below (which is preserved as historical record):
- **Rename `Post` → `Doc`** to avoid clashing with "OP" (original post — the top msg of a Slack thread). `PostThread` → `DocThread`, `PostMessage` → `DocMessage`, `PostFrontmatter` → `Frontmatter`, `PostSyncResult` → `DocSyncResult`.
- **Session : `.md` : staging PC is 1:1:1.** One doc per session, tracked by `SessionState.doc_path`. Multi-doc-per-session was considered and dropped — a "multi-thread doc" is what the 5-thread trainium series is; multiple docs at once is the rare shape, handled by separate session dirs.
- **Two sync verbs**: `sync_doc_staging` (terraform: threads/preamble in state but absent from doc are deleted) and `sync_doc_prod` (additive: nothing deleted; per-channel `prod_threads[channel]` + `prod_preamble_ts[channel]`). `sync_doc_prod` auto-archives the staging PC on success by default; opt out with `keep_staging=True`. Slack has no channel-delete for standard workspaces, so archive is the strongest cleanup available; unarchive is reversible.
- **Channel prefix**: staging PC name is `<prefix><doc_slug>`, where `<prefix>` resolves in order (session override → `THRDS_CHANNEL_PREFIX` env → `""`). Typical usage: `THRDS_CHANNEL_PREFIX=rw-` gives `rw-trainium-update` for user-scoped namespacing on a shared workspace.
- **Ownership durability**: every posted/edited message carries Slack `metadata` (`event_type='thrds'` + `event_payload={session_id, doc_slug, thread_slug, kind}`), so a future `recover` can rebuild `state.json` from `conversations.history` filtered by session_id. Local state is the write-through cache; metadata is the durable source of truth.

All phases landed. Remaining follow-ups (not blockers):

- **Live integration** — drive the CLI against a real Slack workspace end-to-end. Unit tests use spies for the Slack HTTP; only actual round-trips catch edge cases in metadata encoding, foreign-user resolution, PC-creation quirks, and rate-limit shapes.
- **Recover verb** — `thrds recover` scans `conversations.history` filtered on `event_type='thrds'` + a session_id, rebuilds state.json from the Slack-side metadata trail. The metadata stamping already exists; only the CLI + rebuild logic is missing.

## What `thrds` already provides

Worth stating, because the gap is narrower than it looks:

- `ThreadClient` protocol — `list_messages` / `post` / `edit` / `delete`, implemented for Slack, Discord, Bluesky.
- `Thread(messages: list[str])` as declarative desired state, and `sync()` reducing to minimal edit/post/delete `Action`s.
- `Message.editable=False`, so other people's messages in a thread are preserved and never counted against desired slots.
- `SyncOptions.suppress_unfurls=True` — already solves the link-preview noise that makes hand-posting link-heavy drafts unpleasant.
- `linked.py`'s `LinkedThread` — summary messages carrying links to detail messages, with a phase-1/phase-4 split that packs against *placeholder* URLs and rebuilds against real ones once IDs exist. **This is the hard part of cross-linking, already solved.**

## Gap 1: a post is more than one thread

`Thread` models one OP plus replies. The unit actually drafted is *several sibling top-level messages in a channel, each with its own replies*, because that is what serves the reader: a recipient can answer each topic independently, and forward individual threads to different internal teams. A single wall of bullets makes triage their problem.

Proposed:

```python
@dataclass
class Post:
    """Several sibling threads posted into one channel, optionally cross-linked."""
    threads: list[Thread]
    preamble: str | None = None      # a bare top-level message with no replies
```

`sync_post(client, desired: Post, thread_ids: list[str] | None = None) -> PostSyncResult` reconciles each thread by position, reusing `sync()` per thread. Ordering is positional — reordering a post means moving a block, which is the operation that actually happens (the motivating post was reordered twice).

## Gap 2: cross-thread links

Within the motivating post, the MFU thread links to the segfault thread and to the CE thread; those URLs do not exist until after posting. Hand-posting means "post everything, go back, add links, re-edit" — error-prone and the reason the author asked for tooling.

`linked.py` already has the machinery (placeholder pack → real URL rebuild); what it lacks is the *sibling-thread* topology. Generalize its phase split so a reference can name any thread in the `Post`, not only a summary→detail pair. Suggested authoring syntax in the markdown format below: `[text](#thread-slug)`, resolved to a permalink at phase 4.

## Gap 3: a serialization format

One `.md` per post. Two delimiters, because the structure is two-level:

```md
---
channel: C0BCFBDK65S
thread_ts: 1784652206.415649    # optional: post into an existing thread
---

Sorry for the delay @grayjh, updates below:

=== mfu

MFU:

+++

- Previously-quoted ≈6% was one NeuronCore of four.
- See [the segfault thread](#segfault) for why.

=== segfault

`neuronx-cc` segfault in `hlo2penguin`

+++

- [MWE gist](https://gist.github.com/…): one flag flips crash↔compile.
```

- `===` starts a new top-level message; an optional trailing slug names it for cross-links.
- `+++` starts a reply under the current top-level message.
- Text before the first `===` is the preamble.

`---` is deliberately *not* the separator: it collides with YAML frontmatter and markdown `<hr>`, and two levels need two markers anyway. A directory of numbered files (ghpr-style) was considered and rejected — a single document is far nicer to edit and reorder, which is the dominant activity.

## Gap 4: capture — the highest-value piece

The `$c/voice` library works because it holds real **draft → sent** pairs. Today that capture is: screenshot the sent version, hand-transcribe into an example file. That is the step that does not scale, and it is precisely the step that should be mechanical.

`thrds` already has `list_messages`, so pulling a posted `Post` back into the markdown format is nearly free. That turns every edited message into a training pair automatically:

```
draft.md  →  (author edits in Slack)  →  pull  →  sent.md  →  diff = the voice delta
```

Framing this explicitly: it is RLHF for message composition. The library's value scales with the number of pairs, so the marginal cost of capturing one must approach zero.

Note the messages will often be posted **by hand**, not by `thrds`: posting through a Slack app stamps an uneditable "Sent using @Claude" footer, which for external recipients is worse than an inline, in-voice attribution line. So **pull must work against threads `thrds` did not create.** Push is for scratch spaces; pull is the product.

## Why this is worth building at all: the MCP cannot edit

The Claude Slack MCP exposes send / draft / schedule / read / search / react / create — but **no `chat.update` and no delete**. So an agent iterating on a draft in Slack can only *append* replacement messages, which is exactly the mess this spec exists to fix: during the motivating session the self-DM accumulated v2/v3/v4/v5 plus per-thread "proposed replacement" posts, none of which could be cleaned up programmatically.

`thrds`'s `ThreadClient` already has `edit` and `delete`. What it needs is a **user token** (`xoxp-`, `chat:write`): `chat.update` only edits messages authored by the token's owner, so a bot token can edit the app's own posts but not ones the human typed. With a user token, `sync()` reconciles a hand-edited thread against a new desired state in place — the actual workflow — instead of appending.

This is the single highest-leverage capability gap; without it, drafting-in-Slack is append-only.

## Gap 5: a scratch space

Drafting in a self-DM is what the motivating post used, and it was bad: several revisions each became a separate top-level DM, scrolling real history away, with no delete API.

Slack multi-person DMs require ≥2 other people, but a **private channel with a single member is allowed**. So: create `#draft-<slug>`, render the `.md` into it as real messages and threads, edit in place with real rendering, `pull` back, then archive. Disposable and terraformable in a way a self-DM is not.

## Gist mirroring, and whether it wants its own library

`ghpr` mirrors PR/issue bodies and comments to a gist, syncing **via git push to the gist remote** rather than the API — which gets idempotency, diffability, and history for free. `thrds` wants the same for threads.

The shared abstraction is *not* "a gist client". It is:

> **a git-backed mirror providing version history for a canonical upstream that does not expose history through its API** — GitHub PRs and issues surface edit history only in the web UI; Slack does not surface message edit history at all.

That is a genuinely reusable idea with real semantics: one-way mirror (canonical → gist, append-only history) combined with two-way sync against the canonical store (local edits push up, upstream edits pull down). The gist is incidental — it is just the cheapest hosted git repo. The same abstraction would serve a GitLab MR or a Discord thread.

**Sequencing:** implement the mirror inside `thrds` first, shelling out to git as `ghpr` does, then extract once there are two real implementations to compare. A shared API designed against one consumer plus a hypothesis is usually the wrong API; the second implementation is what reveals the true shape. The seam is already visible in `ghpr`'s `src/ghpr/gist.py`, where generic gist CRUD sits alongside `extract_gist_footer`/`add_gist_footer`, which encode ghpr-specific PR-body conventions.

## Non-goals

- Not a general Slack/Discord client. Compose, sync, capture.
- No Block Kit / rich-layout authoring. Markdown in, platform markdown out.
- No scheduling, no cross-channel fan-out.

## Open questions

1. Does the Slack API permit creating a **single-member private channel**, and can a bot post into it? This gates the scratch-space idea entirely.
2. Should `pull` snapshot once, or follow edits over time? Post-publication edits are common, and the interesting voice delta may land minutes later.
3. Cross-platform slugs: Discord and Bluesky have no permalink shape matching Slack's `thread_ts`. Does `#slug` resolution need per-client support in the protocol?
4. Attachments. One `$c/voice` example showed the author moving every number *out* of prose and into attached screenshots. Should the format reference local image paths for upload?
5. Is `Post` better modelled as a first-class type, or as a `list[Thread]` plus a link-resolution pass? The latter is less API surface but leaves the preamble homeless.
