# Add Bluesky client + extras packaging

## Context

`thrds` currently has Slack and Discord clients implementing the `ThreadClient` protocol. The crash-bot in `hudcostreets/nj-crashes` has a working Bluesky posting implementation (`njsp/cli/bsky/`) using `atproto`. A Discord agent (marin-discord / discord-agent) also needs `thrds`.

Goal: add Bluesky as a third platform, package each platform's deps as extras, and make `thrds` the shared library for all three consumers.

## Current state

| | thrds | crashes/njsp |
|---|---|---|
| Slack | ✓ (urllib, zero deps) | ✓ (slack_sdk) |
| Discord | ✓ (curl subprocess, zero deps) | ✗ |
| Bluesky | ✗ | ✓ (atproto) |
| Core sync | ✓ (generic) | ✓ (per-platform, ad hoc) |

thrds' Slack and Discord clients are deliberately zero-dependency (urllib / curl). The Bluesky client needs `atproto` (which pulls in pydantic, httpx, etc.).

## Proposed changes

### 1. Bluesky `ThreadClient` implementation

New file: `thrds/bsky.py`

```python
from atproto import Client as AtClient
from atproto_client.models.app.bsky.feed.defs import PostView
from .core import Message
from .protocol import ThreadClient

class BskyClient:
    def __init__(self, handle: str, password: str):
        self._client = AtClient()
        self._client.login(handle, password)
        self._did = self._client.me.did

    def list_messages(self, thread_id: str) -> list[Message]:
        """Fetch thread posts. thread_id = root post URI (at://)."""
        resp = self._client.get_post_thread(uri=thread_id)
        # Walk reply tree, return in chronological order
        ...

    def post(self, content: str, thread_id: str | None = None) -> Message:
        """Create a post. If thread_id is provided, reply to root."""
        if thread_id is None:
            resp = self._client.send_post(text=content)
        else:
            # Build ReplyRef with root + parent
            resp = self._client.send_post(text=content, reply_to=...)
        return Message(id=resp.uri, content=content)

    def edit(self, message_id: str, content: str) -> Message:
        """Bluesky doesn't support editing posts. Delete + repost."""
        raise EditRateLimited()  # Forces sync to use delete+repost path

    def delete(self, message_id: str) -> None:
        """Delete a post by URI."""
        # Extract rkey from URI
        self._client.delete_post(rkey)
```

Key difference from Slack/Discord: **Bluesky has no edit API**. The `edit()` method raises `EditRateLimited`, which the sync algorithm already handles by falling back to delete+repost. This is the correct behavior — the existing sync logic in `core.py` already supports this path.

#### Bluesky threading model

Bluesky threads use `ReplyRef` with both `root` and `parent` references:
- **Root**: always points to the OP (thread starter)
- **Parent**: points to the immediate parent (for nested replies, but we use flat threads)

For flat threads (crash-bot style), every reply has `root = parent = OP`.

#### Content formatting

Bluesky uses "rich text" via facets (links, mentions, hashtags). The `ThreadClient` protocol takes plain `str` content. Options:
- **Simple**: pass plain text, no links/formatting. Consumers pre-format.
- **Rich text helper**: add a `BskyRichText` helper that builds facet arrays from markdown-ish input. This is platform-specific and belongs in the bsky module.
- **Protocol extension**: add optional `format_content(content: str) -> Any` to `ThreadClient` for platform-specific formatting. Probably overkill.

Recommend: start with plain text. The crash-bot's `slack_str()` and bsky formatting are crash-specific, not library concerns. The consumer builds the desired `Thread(messages=[...])` with platform-appropriate strings.

### 2. Extras packaging

```toml
[project]
dependencies = []  # Core has zero deps

[project.optional-dependencies]
slack = []         # Still zero deps (urllib)
discord = []       # Still zero deps (curl)
bsky = ["atproto>=0.0.46"]
test = ["pytest"]
all = ["atproto>=0.0.46"]  # Convenience
```

Import pattern:
```python
from thrds import sync, Thread           # Core (always available)
from thrds.slack import SlackClient      # No extra deps needed
from thrds.discord import DiscordClient  # No extra deps needed
from thrds.bsky import BskyClient       # Requires: pip install thrds[bsky]
```

The `bsky` import fails gracefully if `atproto` isn't installed (ImportError at import time, not at package level).

### 3. Migrate crashes/njsp to use thrds

After thrds has the bsky client:

1. Add `thrds[slack,bsky]` to crashes' dependencies
2. Replace `njsp/cli/slack/channel_client.py` sync logic with `thrds.sync()`
3. Replace `njsp/cli/bsky/client.py` sync logic with `thrds.sync()`
4. Keep crash-specific formatting (`slack_str()`, bsky facet building) in crashes
5. Keep crash-specific thread lookup (by ACCID metadata) in crashes

The boundary: `thrds` handles "given desired messages, sync the thread." Crashes handles "given a crash, build the desired messages."

### 4. Discord agent consumer

The discord agent uses `thrds.discord.DiscordClient` directly. Same pattern: consumer builds desired `Thread(messages=[...])`, calls `sync()`. The agent handles domain-specific logic (what threads to create, what content to post).

## Implementation plan

1. **Add `bsky.py`** — implement `BskyClient` with `ThreadClient` protocol. `edit()` raises `EditRateLimited`. Add tests with mocked atproto client.
2. **Add extras to `pyproject.toml`** — `[bsky]`, `[all]`, keep slack/discord as empty extras for documentation.
3. **Add Bluesky-specific tests** — test the delete+repost fallback path (already covered in `test_sync.py` but add bsky-specific edge cases like facets, 300-char limit).
4. **Write migration spec for crashes** — separate spec in crashes repo for the actual migration.

## Bluesky-specific edge cases

- **300-char limit** per post (vs Slack 4000, Discord 2000). Long messages need splitting. Should `thrds` handle this, or the consumer? Recommend: consumer splits before building `Thread(messages=[...])`. The library doesn't know content semantics.
- **Rate limits**: Bluesky rate limits are per-DID, ~30 actions/second for most endpoints. The `SyncOptions.pace` parameter handles this.
- **No edit**: handled via `EditRateLimited` fallback.
- **Post lookup**: `list_messages()` needs to walk `getPostThread` response. Bluesky returns a tree, not a flat list. Need to flatten chronologically.
- **Thread creation**: first `post()` (no `thread_id`) creates the root. Subsequent posts use `ReplyRef`. Same as Slack/Discord pattern.

## Open questions

- Should `thrds` handle message splitting (for platforms with char limits)? Or is that consumer responsibility?
- Should `BskyClient` accept an already-logged-in `atproto.Client` instance (for consumers that manage auth separately)?
- Discord agent repo name / location?
