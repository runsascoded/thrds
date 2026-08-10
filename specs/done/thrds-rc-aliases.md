# Ship a sample `.thrds-rc` with shell aliases

**Status:** done (2026-08-10). `.thrds-rc` landed at repo root with the 3 `open` aliases from below plus 2 for verbs added since the spec was drafted (`list-sessions`, `recover`). Cleanup step (removing draft aliases from `~/.rc/git/github/.git-rc`) is a no-op — nothing was ever committed there.

Shell aliases for `thrds` were initially drafted in `~/.rc/git/github/.git-rc` (git-helpers), but that's the wrong home — they're not git/GitHub helpers. Instead, this repo should ship a sample rc file that users (well, Ryan) can source from their dotfiles.

## Change

Add `.thrds-rc` at the repo root:

```bash
# thrds - multi-thread Slack post drafting (https://github.com/runsascoded/thrds)
alias thrdso='thrds open'            # open session's gist in browser
alias thrdsos='thrds open -s'        # open the staging PC
alias thrdsop='thrds open -p'        # open the prod channel
```

Adjust/extend the aliases as appropriate (e.g. if `thrds open` flags have changed, or other subcommands deserve aliases).

## Consumption (already wired, no action needed here)

`~/.private/.thrds-rc` (auto-sourced by `~/.private/.rc`'s `.*-rc` glob) does:

```bash
try_source "$HOME/c/thrds/.thrds-rc" "$HOME/thrds/.thrds-rc"
```

`try_source` (from `~/.rc/.rc`) no-ops on missing files, so this is inert until `.thrds-rc` lands here, and covers both laptop (`~/c/thrds`) and EC2-style (`~/thrds`) clone locations.
