# Read Slack's own parse tree (`rich_text`) instead of re-parsing `text`

## Problem

`mrkdwn.to_markdown` reverse-engineers Slack's formatting from the `text` field with a pile of regexes: `<url|text>`, `*bold*`, `_italic_`, `&amp;`, and now `normalize_fences`. Every one is a heuristic over an **ambiguous, undocumented, lossy** serialization, and each has already needed a hand-tuned guard:

- `_SLACK_ITALIC` carries a load-bearing word-boundary guard because `thread_ts=1&cid=X thread_ts=2` otherwise glues two `_`s into an italic span across half a URL.
- `normalize_fences` cannot distinguish a language tag from a first line that happens to be one word.
- `*bold*` and `_italic_` are applied *inside* fenced blocks, where they should be inert.
- Slack HTML-encodes `&`, so entity decoding has to be sequenced after link rewriting.

These aren't bugs in the regexes; they're the cost of parsing a format that has no grammar. Slack has never published one, and `text` is a rendering of the message, not its structure.

## What's actually available

**Slack returns its own parse tree on every message**, as a `rich_text` block. Verified live against `04-cuda-graph.md` in `#cw-quickwins` (2026-08-19):

```
rich_text
  rich_text_section
    text: "Confirming: I'll delete "               style=None
    text: 'marin/users/marin/grug/cudagraph-gate-*' style={'code': True}
    text: ' (44.6 TiB, 49,360 objects):\n\n'        style=None
  rich_text_preformatted
    text: ' 7.53 TiB  cudagraph-gate-a-pernode-cbon-20260813\n 7.51 TiB  …'
  rich_text_section
    text: '\nEach contains '                        style=None
    text: '…/dev/checkpoints/step-N/d/'             style={'code': True}
    link: '→'
    channel: ''
```

The `rich_text_preformatted` element holds the code block's nine lines **verbatim, with no fences and leading spaces intact**. The entire class of bug the fence spec is about does not exist in this representation — there is nothing to disambiguate.

Element types observed / documented: `rich_text_section`, `rich_text_preformatted`, `rich_text_quote`, `rich_text_list` (with `style: bullet|ordered`, `indent`); leaf elements `text` (with `style: {bold, italic, strike, code}`), `link` (`url` + optional `text`), `user`, `channel`, `usergroup`, `broadcast`, `emoji` (`name` + optional `unicode`).

That covers every construct thrds emits, and it arrives already decoded — no `&amp;`, no `<#C…>` regex, no `<url|text>` regex.

## Ask

Add `thrds/richtext.py`: `rich_text_to_markdown(blocks) -> str`, a pure function over the block tree. Use it in `SlackClient._raw_body` when the message carries a `rich_text` block; keep `to_markdown` as the fallback.

Resolution order in `_raw_body` becomes:

1. a single `section` block → finalized message, body is its text (existing)
2. a `rich_text` block → `rich_text_to_markdown` (new, the common case)
3. otherwise → `to_markdown(text)` (fallback: bot posts, unusual payloads)

`to_slack` (md → mrkdwn) stays regex-based **and stays the source of truth for what we send.** We do not send `rich_text` blocks: any message carrying blocks is uneditable in Slack, which is the whole reason chrome moved back into the text (see `specs/done/per-thread-model.md` and `thrds/chrome.py`). We post plain text and let Slack parse it; we just read its answer instead of re-deriving it.

## Round-trip requirement

Same bar as the fence spec: reading a session back must not rewrite files that nobody edited.

Verified against both live sessions before landing. Every message in the trainium prod thread and both live staging channels was rendered both ways and diffed; the trainium session round-trips **byte-identically** (no commit produced by a `pull -w`).

## Outcome

Implemented as `thrds/richtext.py` (`render`), selected in `SlackClient._message_markdown`, which now resolves a body from three sources in descending fidelity: a lone `section` block (our own finalized posts) → `rich_text` → the flat `text` field.

**Deviation: shipped default-on**, with `THRDS_RICH_TEXT=0` as the escape hatch, rather than opt-in-then-promote. The evidence inverted the risk. The regex path is *actively corrupting data*: `01-cw-quickwins.md` contained

```
`marin/normalized/nemotron_cc_v2{,*1}` … `finetranslations**`
```

where the author wrote ``` `nemotron_cc_v2{,_1}` ``` and ``` `finetranslations_*` ```. `_SLACK_ITALIC` had spanned the two underscores — *across a code span* — turning both into asterisks on a previous pull. Slack's tree has them intact. Leaving the regex path as the default would have meant leaving that in place, so the cautious option was the worse one. The corrected line is the single content change this landed, committed to the cw-quickwins gist.

Things the live data taught that the spec didn't anticipate:

- **`rich_text` fields are HTML-encoded too.** A permalink's `&cid=` arrives as `&amp;cid=` inside `url`, not just in the flat `text`. Caught by a live round-trip that rewrote one trainium link; `render` now applies `decode_entities` exactly where `to_markdown` does.
- **An emoji element may carry no `name`.** A unicode emoji typed directly arrives as `{"type": "emoji", "unicode": "26a0-fe0f"}`, and reading only `name` renders `::`. Falls back to decoding the codepoints.
- **Emphasis spans runs.** Slack splits ``**`x`, and y**`` into a `{code, bold}` run and a `{bold}` run; marking each run separately puts the markers in the wrong place and leaves a dangling pair. Consecutive runs are grouped by their non-code style.
- **A nested list is a *sibling* block**, not a child — `rich_text_list` with `indent: 1` follows its parent. Without an inserted newline every sub-item lands glued to the parent's last line.
- **Slack rewrites `- item` to `• item`** in the stored `text`, which is how `03-cw-mpu.md` ended up with bullet characters. The tree leaves the marker to us, so the local dialect survives.
- **A URL pasted into the composer** is stored as `<url|shortened-display>` with `truncated: true`; the ellipsis label is a rendering artifact, so it renders as a bare URL.

Chrome needed one piece of new machinery: it's authored in Slack mrkdwn (`<#C…>`, `<url|→>`), so it can't be re-parsed out of *rendered markdown* without a second grammar. `chrome.locate` returns its line index instead, and the `richtext` path drops that line by position — always an extremity, so a re-inflated fence in the middle can't shift it.

## Non-goals

- Emitting `rich_text` blocks on send. Uneditable messages are a non-starter for staging.
- A CommonMark parser for the md → mrkdwn direction. `markdown-it-py` would be the reusable choice if we ever want one (it's the maintained CommonMark reference port, and `mdformat` renders its AST back to normalized markdown), but `to_slack` is emitting a *simpler* format than it consumes, which is the easy direction. Not worth a dependency yet.
- Retiring `normalize_fences`. It stays for the `text` fallback.
