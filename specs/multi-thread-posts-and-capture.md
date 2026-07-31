# Spec: multi-thread posts, a markdown format, and draft/sent capture

**Status:** draft, unimplemented. Motivated by a 2026-07-31 Trainium status update to an AWS PM, which took five drafts plus a structural rewrite and ended up as **four top-level Slack messages, each with its own reply thread, cross-linked**. Nothing in `thrds` expresses that shape, and the whole draft→sent capture loop was done by hand from screenshots.

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
