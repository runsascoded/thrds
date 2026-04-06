# Spec: Handle Slack rate limits (429 Too Many Requests)

## Problem

Slack returns HTTP 429 when API calls are too frequent. `sync()` hit this during mass deletion (deleting 34 excess messages rapidly). The response includes a `Retry-After` header indicating how many seconds to wait.

## Fix

`SlackClient._request` should handle 429s with automatic retry:

```python
except HTTPError as e:
    if e.code == 429:
        retry_after = int(e.headers.get("Retry-After", 1))
        time.sleep(retry_after)
        # retry the request
    else:
        raise
```

Also add a small delay (0.3-0.5s) between all mutating API calls in `sync()` to avoid hitting rate limits in the first place. Slack's rate limits are typically:
- `chat.postMessage`: ~1/sec
- `chat.update`: ~1/sec  
- `chat.delete`: ~1/sec (stricter, ~50/min for bulk)
