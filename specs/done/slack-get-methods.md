# Spec: Slack GET methods need query params, not JSON body

## Problem

`SlackClient._request` sends all API calls as POST with JSON body. But `conversations.replies` and `conversations.history` are GET methods that expect query parameters, not a JSON body. Sending them as POST with JSON body returns `invalid_arguments` with "missing required field" errors.

## Fix

`_request` should support GET methods. Either:

1. Add a `method` parameter (`GET` vs `POST`):
```python
def _request(self, endpoint: str, data: dict | None = None, method: str = "POST") -> dict:
    if method == "GET" and data:
        query = "&".join(f"{k}={v}" for k, v in data.items())
        url = f"https://slack.com/api/{endpoint}?{query}"
        body = None
    else:
        url = f"https://slack.com/api/{endpoint}"
        body = json.dumps(data).encode() if data else None
    ...
```

2. Or have `list_messages` use query params directly.

## Affected methods

- `conversations.replies` — used by `list_messages`
- `conversations.history` — would be needed for listing channel messages
