# Slack `editable` flag misses bot-posted messages

## Problem

`SlackClient.list_messages` (in `thrds/slack.py`) marks every message it returns with:

```python
editable=(m.get("user") == bot_uid),
```

where `bot_uid` is the `user_id` returned by `auth.test`. The intent is "mark messages as editable if our bot posted them" — so `sync()` can edit/delete them and leaves foreign human messages alone.

**The bug:** Slack returns `user: null` on `bot_message` events. The bot's identity is in `bot_id`, not `user`. So for our own bot's posts, `m.get("user") == bot_uid` evaluates `None == "U05...RFC"` → `False` → **every message we ourselves posted is marked `editable=False`**.

### Repro

```python
from thrds import SlackClient
c = SlackClient(token=..., channel=...)
# OP was posted by our own bot
messages = c.list_messages(op_ts)
assert all(not m.editable for m in messages)   # every one, including our OP
```

Evidence from a live bot channel:
```
ts 1776557792.086459  user: None  bot_id: B05HUMG4M35
ts 1776559487.520119  user: None  bot_id: B05HUMG4M35
...
```
and `auth.test` returns `"user_id": "U05HUQUPRFC", "bot_id": "B05HUMG4M35"`.

### Downstream effect in `core.sync()`

`sync()` filters:
```python
existing = [m for m in all_existing if m.editable]
```

With every bot message non-editable, `existing = []`, `N = 0`. Then `M > N`, so Phase 3 posts **every** item in `desired` as a fresh reply — including the OP text, which lands as reply #1 inside the thread it was supposed to be syncing. Subsequent runs with the same `desired` re-duplicate.

Observed in `hccs/path`'s daily "no new data" updater: backfilling two days' replies produced a 7-reply → 26-reply balloon (2× OP-as-reply + 2× full replica of all original replies).

## Proposed fix

Also accept `bot_id` match. The bot's own `bot_id` is available from the same `auth.test` call that gives us `user_id`:

```python
@property
def bot_ids(self) -> tuple[str, str]:
    if self._bot_ids is None:
        result = self._request("auth.test", method="POST")
        self._bot_ids = (result["user_id"], result.get("bot_id"))
    return self._bot_ids

def list_messages(self, thread_id: str) -> list[Message]:
    ...
    user_id, bot_id = self.bot_ids
    messages = [
        Message(
            id=m["ts"],
            content=m.get("text", ""),
            editable=(
                m.get("user") == user_id
                or (bot_id is not None and m.get("bot_id") == bot_id)
            ),
        )
        for m in result.get("messages", [])
    ]
    ...
```

Keep the existing `user_id` match — some deployments may post as a real user (chat.postMessage with user token) rather than a bot. The `or` handles both.

## Test plan

1. Post a message via the bot into a test channel.
2. `list_messages` on the thread → assert `messages[0].editable == True`.
3. A human posts a reply in the same thread.
4. `list_messages` again → assert `messages[0].editable == True` (ours) AND `messages[1].editable == False` (theirs).
5. Regression test for the `sync()` duplicate-OP-in-thread symptom: given a live thread with OP + one reply, run `sync(Thread([op, reply, new_reply]), thread_ts=op_ts)` and assert no message text contains the OP template *inside* the thread (i.e. `_fullLayout`… er, `conversations.replies` returns `N + 1` messages, not more).

## Downstream

- `hccs/path`'s `path-data backfill-slack` already works around this with a direct `client.post(thread_id=...)` instead of `sync()`. Once fixed here, that workaround can stay — it's simpler — but the daily `_post_no_new_data` which *does* use `sync()` will start correctly editing the "Polled Nx" count in the OP rather than re-posting it inside the thread.
