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

Same bar as the fence spec, and the one real risk: `rich_text_to_markdown(slack_parse(to_slack(x)))` must equal `x` for every doc already in a session, or the first pull after this lands rewrites files wholesale and every drift check fires.

This is not obviously true — Slack normalizes as it parses (collapsing adjacent styles, its own list/quote handling), so the mapping back to *our* markdown dialect has to be chosen to invert `to_slack`, not to be canonical CommonMark. Gate it:

- Golden-file test over every thread file in the trainium and cw-quickwins sessions: push, read back both ways, require byte equality with the file on disk.
- Ship behind `THRDS_RICH_TEXT=1` for one session's worth of use before making it the default.

## Non-goals

- Emitting `rich_text` blocks on send. Uneditable messages are a non-starter for staging.
- A CommonMark parser for the md → mrkdwn direction. `markdown-it-py` would be the reusable choice if we ever want one (it's the maintained CommonMark reference port, and `mdformat` renders its AST back to normalized markdown), but `to_slack` is emitting a *simpler* format than it consumes, which is the easy direction. Not worth a dependency yet.
- Retiring `normalize_fences`. It stays for path 3.
