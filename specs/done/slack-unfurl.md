# Spec: Suppress Slack link unfurls

## Problem

Slack auto-generates OG link previews ("unfurls") for URLs in messages. This clutters threads with large preview cards for arxiv papers, GitHub PRs, etc.

## Fix

Pass `unfurl_links: false` and `unfurl_media: false` in `chat.postMessage` and `chat.update` calls.

Could be:
1. Always set (simplest)
2. Controlled via `SyncOptions` (e.g. `suppress_unfurls: bool = True`)
3. Per-message option

Option 2 with default `True` is probably best — unfurls are rarely wanted in bot-posted threads.

## Implementation

In `SlackClient.post` and `SlackClient.edit`:
```python
data["unfurl_links"] = False
data["unfurl_media"] = False
```
