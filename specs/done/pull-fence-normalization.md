# `slack pull`: re-inflate code fences to valid CommonMark

## Symptom

A message edited *in Slack* came back through `slck pull --write` as:

```
``` 7.53 TiB  cudagraph-gate-a-pernode-cbon-20260813
 7.51 TiB  cudagraph-gate-c2-pergpu-cbon-20260814
 …
 0.00 TiB  cudagraph-gate-cbase-pergpu-cboff-20260813```
```

Slack renders that fine (mrkdwn fences don't need a newline after the opening ```` ``` ````), but as CommonMark the opening fence swallows the first content line and the closing fence glues to the last one — MD preview and the gist mirror both render it broken (first line missing from the block, trailing ```` ``` ```` shown literally). Seen in `marin-gcs-usage/thrds/cw-quickwins/04-cuda-graph.md` after Ryan compressed the draft in the Slack UI, 2026-08-19.

## Ask

In the mrkdwn→md direction (pull), normalize fences:

- `` ```X `` (opening fence immediately followed by non-newline content) → `` ``` `` + `\n` + `X`. Preserve a same-line *info string* only if it looks like one (single word, no spaces, message authored locally) — content pulled from Slack should always break, since Slack has no info-string concept.
- `` X``` `` (closing fence glued to content) → `X` + `\n` + `` ``` ``.
- Leading space before content on the opening line (`` ``` 7.53 `` → first block line is `` 7.53``) — keep the content byte-exact aside from the inserted newline; don't trim.

Round-trip requirement (fits the existing sync algorithm): push(md→mrkdwn) may compact fences however Slack likes, but `pull(push(x))` must be stable — a file that was valid CommonMark stays byte-identical after a no-edit round trip, so drift detection doesn't fire on formatting.

## Non-goals

Inline code spans (`` ` ``) are fine as-is; only fenced blocks. No attempt to guess languages for highlighting.

## Implementation

`mrkdwn.normalize_fences`, run first in `to_markdown`. Pairs runs of 3+ backticks and inserts newlines only — content is preserved byte-for-byte, so leading whitespace (which inside a code block is data, not indentation) survives.

Deviation from the ask: a first line that looks like an info string is preserved **unconditionally**, not "only if authored locally". `to_markdown` has no provenance to consult, and the round-trip requirement forces it — ```` ```python ```` pushed from a local doc comes back the same way, so breaking it would make every push/pull cycle rewrite the file. The residual ambiguity (a Slack-composed block whose first line is a single bare word, indistinguishable from a language tag) is left attached and documented. A body with no newline at all can't be an info string, so ```` ```foo``` ```` still breaks correctly.

Round-trip verified by parametrized test: `to_markdown(to_slack(x)) == x` for valid CommonMark, and the Slack-composed form converges after one pass.

## Follow-up

This normalizer is a heuristic over `text`, which is the wrong source. Slack returns its own parse tree as `rich_text` blocks on every message, where a code block is a `rich_text_preformatted` element holding the content verbatim with no fences and therefore no ambiguity at all. See `specs/rich-text-ingest.md`. The normalizer stays as the fallback for messages that carry no `rich_text` (our own finalized block posts).
