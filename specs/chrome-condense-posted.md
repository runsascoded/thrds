# Condense posted-thread chrome: one link + ✅

Today a posted thread's staged chrome reads `→ (#marin-alerts) · posted · <gist>` — two links to nearly the same place. Once a real post exists, the `→` target link has little use: the posted permalink already carries the thread root (`?thread_ts=…&cid=…`), so the OP/target is trivially derivable even when the target was a reply-into-thread, and the machine-readable target lives in `thrds.json` regardless.

## Proposal

Once `state == posted`, render:

    ✅ <posted_url|#marin-alerts> · <gist-file link>

- One link: channel name as anchor text, href = OUR posted message's permalink.
- `✅` is the posted-state glyph (replaces the word "posted"). `dropped` could get `🚫` similarly.
- Nothing functional is lost: chrome-as-input (retargeting) already skips terminal threads (`pull_chrome_edits` `is_terminal` check), and the target stays in `thrds.json`.
- Draft threads keep the current `→` form — there the target pointer is the affordance.

## Alternative/complement considered

A ✅ *self-reaction* on the staged copy instead of a chrome glyph — visible at channel-scan level without reading the footer. Fine as an optional extra once [[reactions]] lands (see `specs/reactions.md`), but the chrome glyph should be primary: it survives in the gist mirror and in text-only renderings, reactions don't.
