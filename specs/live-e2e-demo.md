# Live e2e rehearsal / demo package

The 2026-08-19 promote fix was validated by hand-driving a throwaway session end-to-end against a real Slack workspace: `init -G` → `migrate` → `push` (fresh PEC) → `promote` into a bystander-laden thread (expect POST-only) → re-promote with edited content (expect scoped EDIT) → `slck thread` dump asserting bystanders byte-identical. That sequence is worth keeping as a first-class artifact:

1. **`slck demo` (or `thrds slack demo`)**: scripted version of the above in a throwaway PEC, printing each plan and asserting the invariants; archives its channel at the end. Doubles as the "kick the tires" experience for new users — they see push/pull/promote semantics live in their own workspace without touching anything real.
2. **Scheduled/on-demand e2e**: the same script as a rare cron (weekly?) or manual dispatch — not per-commit CI (it needs a real token and makes real API calls). Catches Slack-side behavior drift (API changes, mrkdwn rendering) that unit tests with fake transports cannot.

Sketch: `slck demo [--keep]` — creates `thrds/demo-<date>/`, no gist, runs the sequence, prints a scorecard, archives the PEC unless `--keep`.
