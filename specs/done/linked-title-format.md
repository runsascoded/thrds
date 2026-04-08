# Spec: Use bold linked titles instead of trailing `(details)` links

## Current format

```
- **Title** — summary text. ([details](url))
```

## Desired format

```
- [**Title**](url) — summary text
```

## Why

- Cleaner — no trailing parenthetical
- The title is the natural click target
- Saves ~12 chars per bullet (helps with Discord 2000 char limit)
- Works in both Discord (confirmed: bold inside link anchor renders correctly) and Slack (`<url|*Title*>`)

## Implementation

In `sync_linked`'s summary building, change the bullet format from:
```python
f"- **{section.title}** — {section.summary} ([details]({link}))"
```
to:
```python
f"- [**{section.title}**]({link}) — {section.summary}"
```

For Slack mrkdwn:
```python
f"• <{link}|*{section.title}*> — {section.summary}"
```

## Link length impact

The link markup is the same length (URL chars identical), just positioned differently. No change to split calculations.
