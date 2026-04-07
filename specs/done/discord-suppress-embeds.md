# Spec: Thread `suppress_embeds` through to post/edit calls

## Problem

`DiscordClient.sync()` accepts `suppress_embeds=True` but doesn't pass `flags: 4` to `post()` or `edit()` calls. Messages get posted/edited without embed suppression, causing link previews to appear.

## Fix

When `suppress_embeds` is set in `SyncOptions`:
- `post()` should include `"flags": 4` in the payload
- `edit()` should include `"flags": 4` in the payload

Either:
1. Add `flags` parameter to `post()` and `edit()`, and have `sync()` pass it
2. Or store `suppress_embeds` on the client instance and apply automatically

Option 1 is cleaner:

```python
def post(self, content: str, thread_id: str | None = None, flags: int = 0) -> Message:
    data = {"content": content, "flags": flags}
    ...

def edit(self, message_id: str, content: str, flags: int = 0) -> Message:
    data = {"content": content, "flags": flags}
    ...
```

And in `sync()`, pass `flags=4` when `opts.suppress_embeds`.
