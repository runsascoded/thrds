# Spec: `sync_linked` edit-back needs pacing

## Problem

`sync_linked` passes `pace` to the initial `sync()` call, but the edit-back phase (line ~191) calls `self.edit()` in a tight loop without any sleep. On Discord, this hits the old-message edit rate limit.

## Fix

Add `time.sleep(pace)` between edit-back calls, same as the initial sync:

```python
for i, (msg_id, content) in enumerate(zip(summary_msg_ids, final_summaries)):
    if i > 0 and pace > 0:
        time.sleep(pace)
    self.edit(msg_id, content)
```

Or reuse the existing rate-limit retry logic from `EditRateLimited` handling.
