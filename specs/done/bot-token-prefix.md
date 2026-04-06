# Spec: Handle Discord Bot token prefix

## Problem

Discord bot tokens need `Bot ` prefix in the `Authorization` header. Currently `DiscordClient` passes the token as-is, requiring callers to prepend `Bot `:

```python
DiscordClient(token=f"Bot {os.environ['DISCORD_TOKEN']}", ...)
```

## Fix

`DiscordClient.__init__` should auto-prepend `Bot ` if the token doesn't already start with it:

```python
def __init__(self, token: str, channel_id: str):
    self.token = token if token.startswith("Bot ") else f"Bot {token}"
    ...
```
