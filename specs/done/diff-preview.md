# Colored diff preview for sync actions

## Problem

`thrds.sync()` returns a `SyncResult` with a list of `Action` objects describing what it will do (or did). Each action has `type` (`POST`/`EDIT`/`SKIP`/`DELETE`), `index`, `message_id`, and `content` (the desired text, for non-delete actions).

This is sufficient for the engine, but not for *previewing* what sync will do. To show a user a colored diff like:

```
  14212: EDIT [0]
         -old message text here...
         +new message text here...
  14212: POST [1]
         +entirely new message...
  14378: SKIP [1] (unchanged)
  14433: DELETE [3]
         -message being removed
```

the action alone doesn't have the prior content — only the new content. The caller has to correlate actions with the `existing` list passed to `sync()` to render an edit diff.

## Prior art

Downstream `hccs/crashes` (`njsp/cli/slack/channel_client.py`) had a hand-rolled colored diff before migrating to `thrds`:

```python
log(f"text doesn't match:\n{RED}-{text}\n{GREEN}+{new_text}")
log(f"new message:\n{GREEN}+{new_text}")
log(f"deleting extra message:\n{RED}-{msg.text}")
```

The `thrds` migration (simpler engine API) dropped this capability. It should be restored, factored into `thrds` so every downstream consumer (Slack, Bluesky, future clients) gets it for free.

## Proposal

### 1. `Action.prior_content: str | None`

Add a new field to `Action`:

```python
@dataclass
class Action:
    type: ActionType
    index: int
    message_id: str | None = None
    content: str | None = None          # desired/new text (for POST, EDIT, SKIP)
    prior_content: str | None = None    # existing text (for EDIT, DELETE)
```

Populate it in `sync()`:
- **EDIT**: `prior_content=existing[i].content`
- **DELETE**: `prior_content=existing[i].content`
- **POST**: leave `None` (no prior)
- **SKIP**: leave `None` (or set both — tbd, but minimal is fine since `content` already has it)

### 2. `Action.format(color: bool = True) -> str`

Per-action formatter:

```python
def format(self, color: bool = True) -> str:
    RED, GREEN, RESET = ("\033[31m", "\033[32m", "\033[0m") if color else ("", "", "")
    header = f"{self.type.value.upper()} [{self.index}]"
    match self.type:
        case ActionType.POST:
            return f"{header}\n  {GREEN}+{self.content}{RESET}"
        case ActionType.EDIT:
            return f"{header}\n  {RED}-{self.prior_content}{RESET}\n  {GREEN}+{self.content}{RESET}"
        case ActionType.DELETE:
            return f"{header}\n  {RED}-{self.prior_content}{RESET}"
        case ActionType.SKIP:
            return f"{header} (unchanged)"
```

Multi-line `content` gets a `-` or `+` prefix on each line (proper unified-diff style).

### 3. `SyncResult.format_preview(color: bool = True, prefix: str = "") -> str`

Convenience aggregator for rendering the whole result:

```python
def format_preview(self, color: bool = True, prefix: str = "") -> str:
    return "\n".join(prefix + a.format(color=color) for a in self.actions)
```

The `prefix` lets callers prepend per-thread context (e.g. `"14212: "` in crashes, or a bsky thread URL).

## Non-goals

- **Unified diff ranges (`@@ -1,3 +1,3 @@`)**: overkill for typical thread messages (short, single-block). If needed later, add `Action.format(unified=True)` using `difflib.unified_diff`.
- **Terminal-width wrapping**: let the caller or `rich`/similar handle.
- **HTML output**: keep this ANSI-only. If downstream wants HTML, add a separate renderer.

## Backward compat

Adding a new field with a default of `None` is compat. Callers not using `prior_content` or `format()` are unaffected. Existing `Action(type=..., index=...)` constructions still work.

## Downstream follow-up

After this lands in `thrds`:

1. `hccs/crashes` `njsp/cli/slack/channel_client.py:376` — replace the bare `err(f"{BLUE}{accid:>5d}: {prefix}{action.type.value} [{action.index}]{RESET}")` with `err(action.format(color=True))` (or iterate `result.format_preview(prefix=f"{accid:>5d}: ").splitlines()` and color each line with the accid in BLUE).

2. `hccs/crashes` and future Bluesky sync — same formatter applies.
