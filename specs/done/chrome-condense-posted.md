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

## Outcome (implemented 2026-08-19)

Implemented as proposed. `✅ <posted-permalink|#channel> · <gist-file link>` once `state == posted`; drafts keep `→`.

**`render` needed a channel name, because Slack won't put a `<#C…>` mention in link text.** So the anchor has to be a literal string, and `render` grew a `channel_name` param rather than deriving one — it's called once per thread per push and must stay offline. Names are resolved by a new `SlackClient._resolve_channel_names`, alongside `_target_urls` in `_chrome_for_threads`, and cached in `thrds.json` as `channel_names: {id: name}` (display-only, so a channel rename just goes stale). One `conversations.info` per channel per session — not `list_channels_by_name`, which paginates `conversations.list` across the whole workspace and needs `channels:read`. Failures are swallowed like `_target_urls`': the footer degrades to the three-part draft form rather than failing a push.

**`parse` had to learn the form, or `split` would leak it into content.** The spec framed chrome-as-input as a non-issue because `pull_chrome_edits` skips terminal threads — true, but that's the *retarget* path. The *stripping* path is separate and unconditional: `pull_threads_staging` reads every staged thread including posted ones, so a footer `parse` rejects survives into the pulled markdown as a stray line. `✅ <url|#name>` is now an anchor form of its own; the channel id rides along in the permalink, so the name is only ever display text.

**`thread_ts` is deliberately *not* recovered from the posted permalink**, though `_ts_from_permalink` would happily produce one. That URL is our own message's; for a thread we started it would name our OP as the thread to reply into — a fact invented by rendering, which the target never asserted.

**Both spellings of the glyph parse.** Whether Slack stores `✅` or echoes `:white_check_mark:` isn't contractual, and `_reconcile_chrome` compares rendered footer against live footer as *text* — so a spelling flip would read as permanent drift and re-edit every OP on every push, forever. `chrome.GLYPH_ALIASES` + `normalize_glyphs`, applied in `_live_chrome` next to the existing entity-decode (which exists for exactly this failure mode with `&amp;`).

**`dropped` did not get `🚫`.** The spec floated it as a maybe; unlike `posted` there's no URL to hang it on, so it'd be a bare glyph swap needing `state` threaded into `render`. Left alone.

Verified live (read-only) against `cw-quickwins`: the three `posted` threads render the condensed form with `#marin-alerts` resolved from the API, `cw-quickwins` (draft) and `cw-summary` (dropped) keep theirs. That check caught a real bug — `conversations.info` answers a JSON POST with `invalid_arguments` and needs the urlencoded GET, the same constraint `list_channels_by_name` already documents. The unit test's fake `_request` accepted any method, so it passed green while the live call failed silently into the swallow; the fake now pins `method='GET'`.

Existing posted threads re-render on the next push: `_reconcile_chrome` compares live against desired chrome, so the shape change converges on its own.

20 tests (`test_chrome.py`, `test_chrome_sync.py`); suite at 939.
