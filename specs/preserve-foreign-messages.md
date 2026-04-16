# Preserve non-bot ("foreign") messages when syncing threads

## Problem

`thrds.core.sync()` does a naive index-based reconcile between `existing[i]`
and `desired[i]`. It has no awareness of message authorship: if another user
(human, different bot) has posted into the thread, sync will either:

- Try to `edit` a message it doesn't own → Slack returns
  `cant_update_message`, the whole sync aborts
- Try to `delete` a message it doesn't own → same class of failure

Downstream impact: the `nj-crashes` project's daily Slack sync was blocked for
5+ days because a human shared a news article in a crash thread (accid 13908),
and `thrds.sync` subsequently refused to operate on that thread.

The pre-`thrds` Slack sync code handled this correctly — it filtered
`msgs[msgs.user == bot_uid]` before any edit/delete. That property was lost in
the migration to `thrds`.

Moreover, encouraging humans to interject in threads (sharing related news,
commentary, etc.) is a desirable product behavior — not a pathology to work
around. The bot should coexist with human interjections, maintaining *its*
messages while leaving everyone else's alone.

## Proposal

### 1. `Message` gains an `editable: bool` flag

```python
@dataclass
class Message:
    id: str
    content: str
    editable: bool = True  # False → not owned by the sync client, leave alone
```

Default `True` preserves existing behavior for implementations that haven't
wired up authorship yet.

### 2. Client `list_messages` populates `editable`

Each platform determines editability differently:

- **Slack**: cache `auth.test` once per client; set
  `editable = (raw_msg.get("user") == bot_user_id)`. A bot can only
  `chat.update` / `chat.delete` its own messages.
- **Discord**: `editable = (raw_msg.author.id == current_user_id)`.
- **Bluesky**: posts are per-account; every returned post in its own thread
  is editable iff authored by the session DID. (Already implicitly true via
  the per-account thread model, but make it explicit.)

### 3. `sync()` operates on the editable projection

Conceptually, the bot's "slots" in the thread are the editable messages, in
order. Non-editable messages are invisible to the matching logic (but are
never deleted and never edited):

1. Partition `existing` into `editable_existing` (editable slots, in thread
   order) and `foreign` (everything else — preserved in place).
2. **Phase 1 (delete extras):** if `len(desired) < len(editable_existing)`,
   delete the tail *editable* messages by `msg.id`. Never touch `foreign`.
3. **Phase 2 (edit overlap):** for `i in range(min(len(desired),
   len(editable_existing)))`, compare `editable_existing[i].content` to
   `desired[i]` and edit if needed. Address the edit by `msg.id`.
4. **Phase 3 (post new):** if `len(desired) > len(editable_existing)`, post
   the remainder. Slack/Discord append to the thread end, which means new
   messages naturally appear *after* any human interjection. This is fine —
   threads chronologically interleave bot-maintained state with human
   commentary.

### 4. `SyncResult` unchanged

The `actions` list still describes the mutations the client performed. Foreign
messages generate no actions (they were neither touched nor considered).

## Tradeoffs & non-goals

- **Bot messages may become non-contiguous.** If a human posts between bot
  message 2 and bot message 3, and later a new prev-version is added, it will
  land at the thread end (after the human) rather than immediately after bot
  message 2. This is a platform limitation — threads don't allow insertion —
  and is consistent with how humans expect threads to work.
- **No reordering.** If the bot wants to re-present its state in a different
  order, it still can't — only append.
- **Foreign-author detection is per-client.** No global "who am I" abstraction
  is being added; each client handles its own identity check.

## Test plan

Add `tests/test_foreign_messages.py` covering:

1. Thread with `[bot, bot, human, bot]`, desired `[bot', bot', bot']`:
   - Edit slot 0 → bot', edit slot 1 → bot', edit slot 2 → bot' (the 4th
     existing, not the human)
   - Human message untouched
2. Thread with `[bot, bot, human, bot]`, desired `[bot]`:
   - Edit slot 0 → bot, delete editable slots 1 and 2 (the 2nd and 4th
     existing); human untouched
3. Thread with `[bot, human]`, desired `[bot, bot2]`:
   - Edit slot 0 → bot (nop), post bot2 at end
4. Thread with `[human, bot, human]`, desired `[bot']`:
   - Edit slot 0 (the 2nd existing) → bot'; both humans untouched

Unit tests should use a fake `ThreadClient` impl that tracks `edit`, `delete`,
`post` calls by `msg.id`.

Also add one Slack-specific integration-style test (mocked `_request`) that
verifies `list_messages` correctly tags editability via `auth.test`.

## Future work (not in this change)

- **Version labels.** Each bot message could carry a `v1`, `v2`, ..., `vN`
  prefix so it's obvious that the OP is `vN` and thread replies climb from
  `v1` toward `vN`. Useful when humans interleave and the version history
  becomes visually non-contiguous. Track in a separate spec.
