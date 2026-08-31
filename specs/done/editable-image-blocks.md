# Editable image blocks in thread docs

## Motivation

Automated digests want a thread OP that carries a **chart/card image which updates
in place** as the underlying data changes — e.g. a daily-refreshed usage card at
the top of a monthly thread. Slack makes this possible **only** via a Block Kit
`image` block: an *attached file* (`files.upload`) can't be edited, but an
`image` block's `image_url` can be swapped by `chat.update`. thrds already sends
every message as explicit `blocks` (a `section` with mrkdwn — `slack.py:486-502`)
and re-sends them on every converge, so an `image` block is a natural second
block type that rides the existing sync/diff machinery.

This is a general capability (any thrds thread can then carry a live image), and
a genuine differentiator over hand-rolled `chat.postMessage` usage: the image
becomes *declarative* — you point a doc at a URL, and thrds keeps the rendered
block converged.

The first consumer is the `marin-gcs-usage` monthly usage digest (its OP wants a
per-day table + sparkline PNG that refreshes on every scan); see that repo's
`specs/slack-digest-shape-c.md`.

## Feature

### Doc syntax

A standalone Markdown image line in a thread doc (or a thread reply) becomes an
`image` block appended after the message's `section`:

```
![alt text](https://host/card.png)
```

- Must be on its own line (a paragraph), so it's unambiguous vs. inline mrkdwn.
- `alt text` → the block's required `alt_text`. Empty alt is allowed but a lint
  warning (Slack requires the field; accessibility).
- Multiple image lines in one message → multiple `image` blocks, in order.
- Anything else on the message stays in the `section` mrkdwn as today; the image
  lines are lifted out into their own blocks.

### Round-trip (`md.py` + `slack.py`)

- **`parse_doc` / `serialize_doc`**: recognize the image line as a distinct node
  in a message body, so a doc → wire → doc round-trips (the image line survives
  `serialize`, and `fetch` reconstructs it).
- **Send** (`slack.py` message builder): emit
  `{"type": "image", "image_url": <url>, "alt_text": <alt>}` blocks after the
  `section`. `chat.update` already re-sends the full `blocks` array, so an
  edited/added/removed image converges with no new code path.
- **Fetch / read-back** (`render_rich_text` neighborhood, `slack.py:306-313`):
  translate an incoming `image` block back to the `![alt](url)` line, so a `pull`
  round-trips it into the doc.

### Editability & cache-busting

The point is the *same logical image, new content*. Two cases:

1. **URL changes** (e.g. the caller versions the file: `card-202608.png?v=<scan>`
   or a content-hash path). thrds's existing text-diff already sees the changed
   URL → `chat.update`. This is the MVP: **the caller owns the version token**;
   thrds just diffs the block and updates when the URL string changes. No thrds
   state needed.

2. **Same URL, changed bytes** (Slack caches by URL and won't refetch). thrds
   should offer a **bust** affordance so a doc author doesn't have to hand-roll a
   query param. Proposed opt-in marker:

   ```
   ![alt](https://host/card.png){bust}
   ```

   With `{bust}`, on each converge thrds appends/refreshes a `?v=<token>` on the
   `image_url` **only when the message is otherwise being updated** (so a no-op
   converge stays a no-op and doesn't churn the image every run). `<token>` = a
   short monotonic value (unix-min or a caller-provided `--bust-token`). Without
   `{bust}`, thrds leaves the URL verbatim (case 1). Keep case-1 as the default;
   `{bust}` is the convenience for callers who can't easily version their URL.

   (Decide during impl whether `{bust}` state — the last token — needs to live in
   `thrds.yml` or can be re-derived; re-derivable is simpler.)

### Bot-token guard (small, related)

thrds is token-agnostic (bot `xoxb` or user `xoxp`) and should stay so. But
**per-message sender customization — `username` / `icon_url`/`icon_emoji`
overrides — only works with a bot token** (Slack ignores them for user tokens).
Today that fails silently (the override is dropped). Add a **clear error** when a
session/doc requests a per-message sender override while authenticated with a
user token: fail closed with a message naming the offending field and telling the
caller to use a bot token. Image blocks themselves work with either token — this
guard is specifically about sender overrides, which Shape-C-style digests rely on
(headline-as-sender-name + custom avatar). (Cross-ref `specs/done/bot-token-prefix.md`.)

## Constraints (Slack)

- `image_url` must be a publicly reachable HTTPS URL Slack's fetchers can GET
  (CORS-open helps but Slack fetches server-side; the host must not gate it).
- `alt_text` is required; image dimensions/host size limits apply (Slack scales).
- An `image` block in a *reply* is allowed, but the primary use is the OP.

## Out of scope

- Rendering/hosting the image (that's the caller's job — thrds only delivers the
  block and keeps it converged).
- Non-image blocks (video, file) — can generalize later if a need appears.

## Acceptance

- A doc with an `![alt](url)` OP line posts an OP carrying an `image` block;
  changing the URL and `push`ing does a single `chat.update` swapping it; `pull`
  round-trips the line; a no-op converge writes nothing.
- `{bust}` refreshes `?v=` only on updates that already touch the message.
- Requesting a `username`/`icon` override under a user token errors clearly.

## Implemented (2026-08-31)

Everything above, with these deltas:

- **`md.py` untouched.** Message content is an opaque string at the doc layer;
  `![alt](url)` lines round-trip through `parse_thread`/`serialize_thread`
  verbatim with no new node type. The real work is at the wire boundary:
  `thrds/imageblock.py` (pure helpers: `ImageRef`, `split_trailing_images`,
  `to_block`/`from_block`, bust-URL handling) + `SlackClient.post`/`edit`
  (lift) + `SlackClient._message_markdown` (read-back).
- **Trailing-only lifting.** Only the trailing run of standalone image lines
  (blank lines allowed between) is lifted into blocks; a mid-message image
  line stays literal text. Read-back appends reconstructed lines at the end
  of the body, so trailing-only is what makes converge idempotent — a
  mid-message image would round-trip to a different doc and re-edit forever.
- **`{bust}` uses `?thrds_bust=<token>`, not `?v=`.** Read-back has to strip
  the param and re-emit the `{bust}` suffix (else every converge would see a
  diff); a plain `v` param would be indistinguishable from a caller-versioned
  URL and stripping it would corrupt case-1 usage. Token is unix-minutes,
  re-derived per post/edit — no `thrds.yml` state (the re-derivable option).
  A SKIP never mints a token, so no-op converges stay no-ops (pinned by
  `test_post_then_readback_round_trips_content`).
- **Empty alt**: sent as-is (substituting a placeholder would break read-back
  convergence) with a `UserWarning` at block-build time, rather than a lint
  rule — there's no Slack linter surface yet.
- **Image-only messages raise** (Slack rejects an empty `section`); body over
  the 3000-char section limit alongside images also raises.
- **Staging-chrome interplay**: no chrome → `[section, *images]`; draft
  chrome → footer stays folded in the section text (pull strips it via
  `split_chrome`, and the flat-text fallback keeps `_live_chrome` reporting
  draft); finalized chrome → images slot between the section and its
  `context` footer.
- **Declarative blocks on edit**: a markdown-mode `chat.update` with no
  images sends `blocks: []`, so removing a doc's image line removes the live
  block (`chat.update` otherwise preserves existing blocks). Raw-mode
  (`raw=True`) posts/edits are untouched — no lifting, no clearing.
- **Bot-token guard** is in `SlackClient.post`: any resolved
  `username`/`icon_url`/`icon_emoji` (message override or client default)
  under an `xoxp-` token raises a `ValueError` naming the fields; other
  token shapes are unaffected.

Tests: `tests/test_imageblock.py` (pure helpers), `tests/test_image_blocks.py`
(wire payloads, chrome interplay, read-back round-trips, guard).
