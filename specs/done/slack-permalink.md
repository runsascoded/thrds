# Spec: Slack permalink helper

## Problem

Slack message permalinks within threads need `thread_ts` and `cid` query params to resolve correctly. Without them, the link 404s or lands on the wrong message.

## Fix

Add a `permalink` method to `SlackClient`:

```python
def permalink(self, message_ts: str, thread_ts: str | None = None) -> str:
    """Build a Slack permalink URL for a message."""
    ts_compact = message_ts.replace(".", "")
    url = f"https://{self.workspace}.slack.com/archives/{self.channel}/p{ts_compact}"
    if thread_ts:
        url += f"?thread_ts={thread_ts}&cid={self.channel}"
    return url
```

## Implementation

Used `chat.getPermalink` API (GET) rather than manual URL building. Avoids needing
a `workspace` parameter. Costs one API call per link, but these are read-only and
infrequent.
