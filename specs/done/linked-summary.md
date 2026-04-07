# Spec: Linked summary messages with deferred ID resolution

## Problem

A common pattern when posting threaded content: a "summary" message at the top links to "detail" messages posted after it. But the detail message IDs don't exist yet when the summary is composed. Currently callers must:

1. Pre-compute message splits using placeholder link lengths
2. Post detail messages first (or post summary without links)
3. Edit the summary to insert real links
4. Handle platform-specific char limits and link formats
5. Split the summary across multiple messages if links push it over the limit

This is complex, error-prone, and duplicated across consumers.

## Solution

`thrds` should support a `LinkedThread` (or similar) that declaratively describes a summary + details structure, handles the full post-then-edit-back lifecycle, and splits messages as needed.

## API

```python
from thrds import LinkedThread, Section

thread = LinkedThread(
    summary_prefix="# Weekly Digest: Mar 24–30, 2026\n298 msgs...",
    sections=[
        Section(title="MoE Training", summary="Linear decay +0.03 BPB", body="Full MoE content..."),
        Section(title="Infrastructure", summary="4 Iris restarts", body="Full infra content..."),
        # ...
    ],
)

# Sync handles everything:
# 1. Computes detail messages from section bodies (splitting long ones)
# 2. Computes summary messages from section titles+summaries, reserving
#    space for links of known max length per platform
# 3. Greedy-splits summary across messages if needed
# 4. Posts/edits all messages via thrds.sync()
# 5. Edits summary messages back with real detail message links
result = discord.sync_linked(thread, thread_id="...")
result = slack.sync_linked(thread, thread_ts="...")
```

## Key behaviors

### Link format per platform

Each client knows its own link format and max length:
- `DiscordClient.detail_link(msg_id, thread_id)` → `([details](https://discord.com/channels/G/T/M))` (101 chars)
- `SlackClient.detail_link(msg_ts)` → `(<permalink|details>)` (~123 chars, via `chat.getPermalink`)

### Summary splitting

Given N sections with known link overhead per platform:
- Greedy pack: add bullets to current summary message until next bullet + link would exceed limit
- Start a new summary message
- Discord (2000 limit): typically splits into 2 messages for 10+ sections
- Slack (4000 limit): usually fits in 1

### Detail message splitting

Long sections (e.g. MoE with lots of activity) may exceed the per-message limit. Split on paragraph boundaries within a section. The summary link points to the first message of the section.

### Link matching

Match summary bullets to detail messages by section title (header-based), not positional index. This is robust to detail messages being split across multiple messages.

### Sync lifecycle

1. Build desired detail messages from sections (split long ones)
2. Build desired summary messages (with placeholder links for length calculation)
3. `sync()` the full message list: [summary_msgs...] + [detail_msgs...]
4. After sync, resolve real message IDs for each section's first detail message
5. Rebuild summary messages with real links
6. Edit summary messages with final content

Steps 1-3 and 4-6 are two `sync()` calls, or `sync()` + targeted edits.

## Data model

```python
@dataclass
class Section:
    title: str          # Bold title in summary bullet
    summary: str        # One-line summary for S bullet
    body: str           # Full markdown content for M message(s)

@dataclass
class LinkedThread:
    summary_prefix: str          # Content before the bullets (XS headline, etc.)
    sections: list[Section]
    summary_suffix: str = ""     # Optional footer after bullets
```

## What stays in the consumer (summarize.py)

- LLM prompt and generation
- Platform-specific content conversion (md→mrkdwn, viewer→discord URLs, channel mention resolution)
- Storing/loading `meta.json`

## What moves to thrds

- `LinkedThread` / `Section` data model
- Summary message building with link reservation
- Greedy message splitting
- Detail message splitting on section boundaries
- Post-then-edit-back lifecycle
- Header-based link matching
