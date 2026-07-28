# `sync_linked` phase-4 repack: fewer-messages case raises

Follow-up to [transport-robustness]. The phase-4 hardening (padded `_detail_url_placeholder` + count check + `strict=True` zip) correctly prevents the silent-truncation bug in the *more*-messages direction, but the *fewer*-messages direction now raises — and it's reachable in the common path, not just the boundary.

## Repro
Because the 180-char placeholder is an upper bound, real permalinks (~110-140 chars) are always shorter, so the phase-4 greedy repack can merge bullets into fewer messages than phase 1 posted:

```python
from thrds.linked import build_summary_messages, LinkedThread, Section
from thrds.slack import SLACK_MESSAGE_LIMIT, SlackClient

bullet = SlackClient.__dict__["_bullet"]
secs = [Section(title=f"Topic {i:02d}", summary="w" * 40, body="b") for i in range(21)]
lt = LinkedThread(summary_prefix="*Weekly summary*", sections=secs)
p1 = build_summary_messages(lt, ["x" * 180] * 21, SLACK_MESSAGE_LIMIT, bullet_fn=bullet)
p4 = build_summary_messages(lt, ["https://openathena.slack.com/archives/C0AQC2VKEJF/p1234567890123456?thread_ts=1234567890.123456&cid=C0AQC2VKEJF"] * 21, SLACK_MESSAGE_LIMIT, bullet_fn=bullet)
assert (len(p1), len(p4)) == (2, 1)  # sync_linked raises RuntimeError on this
```

21 sections × ~40-char summaries is a realistic weekly-digest shape — this isn't an edge case. Any digest whose phase-1 packing lands near a message boundary will crash in phase 4, which is the same "summary generated but never posted/committed" failure mode the original spec set out to eliminate.

## Fix: preserve the phase-1 partition instead of repacking globally
Phase 4 should not re-run greedy packing at all. Instead, keep the bullets→messages assignment computed in phase 1 and substitute real URLs *within* each message's bullet group:

- Have `build_summary_messages` (or a sibling) return the partition — which bullet indices landed in which message — alongside the messages.
- Phase 4 rebuilds each phase-1 message from its own bullet group with real URLs. Since every real URL is ≤ the placeholder length, each rebuilt message can only shrink; the count is identical by construction, and every message stays ≤ limit.
- The count-mismatch `RuntimeError` and `strict=True` zip then become true invariant assertions (unreachable absent a bug) rather than a reachable failure mode, and the "bump `_detail_url_placeholder`" advice in the error message becomes accurate again.

The equivalent phase pairing in `DiscordClient` (if it shares the placeholder/repack pattern) should get the same treatment.

## Tests
- The repro above as a `sync_linked`-level test (fake transport): phase-1 count 2, and the final edits succeed with 2 messages whose contents are asserted exactly (per root testing conventions — exact equality on the rebuilt messages, not substring checks).
- Partition-preservation unit test: same partition in, real URLs substituted, every message ≤ limit, count unchanged.
- TFFP: repro test red on current HEAD (`1c55c9e`), green with the fix.

[transport-robustness]: done/transport-robustness.md
