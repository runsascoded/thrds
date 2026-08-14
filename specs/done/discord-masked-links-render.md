# Discord lint fix: masked links DO render in user messages

Correction to `done/discord-platform.md` (my own spec error, 2026-08-14): it claimed masked
links (`[text](url)`) "render as literal text in normal user messages" and the implemented
`DiscordLinter` + tests enforce that as an error. **Outdated** — Discord has rendered masked
links in regular user messages since its 2023 markdown update (with a click-through warning
for untrusted domains). In-the-wild confirmation from the marin server: Eric Czech's
2026-08-13 `#internal-discuss` message uses `[#117](https://github.com/marin-community/…)`
style links — users write them because they render.

Change:
- Downgrade the masked-link rule from error to **info/off**; optionally keep a *warning* for
  very long **bare** URLs (the actual aesthetic problem masked links solve).
- Update `test_lint.py` accordingly; note the correction in `done/discord-platform.md`.

## Implementation notes (2026-08-14)

- Chose **off** rather than downgrading to info: the rule was a false positive against a
  construct Discord authors *should* keep using. Kept the code path minimal — a live
  info-only rule that fires on every masked link but says "this is fine actually" would be
  noise.
- Did **not** add the optional long-bare-URL warning. Deferring until a real doc actually
  runs into an ugly-URL rendering — easy to add later, avoids inventing a threshold
  (100 chars? 200?) without a live example driving the choice.
- `_MASKED_LINK_RE` stays in `thrds/lint.py` — still used by `BskyLinter`, where the
  original claim *is* accurate (bsky auto-linkifies bare URLs via facets, and
  `[text](url)` renders as literal text).

Diff shape:
- `thrds/lint.py`: dropped the masked-link loop from `DiscordLinter.lint()`; updated the
  module + class docstrings; pointer to this spec in the module docstring.
- `thrds/cli.py`: dropped masked-link from `discord_cli` and `discord_lint` docstrings +
  the section comment; pointer to this spec.
- `tests/test_lint.py`: removed 4 discord masked-link tests (`test_masked_link_flagged`,
  `test_multiple_masked_links_on_one_line`, `test_masked_link_inside_code_fence_ignored`)
  and reshaped `test_report_sorted_by_line_then_column` / `test_report_format_prefixes_path_when_given`
  to not depend on the removed rule. Left a stub `test_masked_link_not_flagged` that
  documents the correction (and pins the desired non-behavior).
- `tests/test_discord_cli.py`: swapped masked-link fixtures for raw-@mention + table
  fixtures in `test_discord_render_autoruns_lint_and_prints_warnings_to_stderr`,
  `test_discord_render_no_lint_flag_skips_the_warning_pass`, and
  `test_discord_lint_reports_issues_to_stderr`.
- `README.md`: reworded the `thrds discord …` blurb ("tables and raw `@name` — two
  constructs…"); noted that masked links do render since Discord's 2023 update.
- `specs/done/discord-platform.md`: struck the masked-link rule with a pointer here.
