# ghpr

"Clone" GitHub PRs/issues, locally edit title/description/comments, "push" back to GitHub, and mirror to Gists.

[![ghpr-py](https://img.shields.io/pypi/v/ghpr-py?label=ghpr-py)](https://pypi.org/project/ghpr-py/)

- Sometimes PR and issue descriptions/comments warrant more complex editing than GitHub's web UI comfortably allows.
- `ghpr` lets you "clone" PRs and issues locally as Markdown files (including titles and comments), so you can edit them with your favorite IDE, then "push" updates back to GitHub.
- `ghpr` also mirrors PR/issue content to Gists, for version control and easy sharing / backing up / syncing across machines.

**Examples:**
- [marin#1773]: issue with complex description and comments ([e.g.][1773 comment]); mirrored to [this gist][1773 gist]
- [marin#1723]: PR with complex description, mirrored to [this gist][1723 gist]

## Features

- **Clone** PR/Issues locally with comments
- **Sync** bidirectionally between GitHub and local files
- **Diff** local changes vs remote (with ownership warnings for others' comments)
- **Fetch/pull/push** with a real merge base, so concurrent local and remote edits both survive
- **Gist mirroring** for version control and sharing
- **Comment management** - edit and sync PR/issue comments
- **Draft comments** - create `new*.md` files, push to post as comments
- **Shell integration** - aliases and tab completion for subcommands, flags, and options

## Installation

```bash
pip install ghpr-py
```

## Usage

### Basic Workflow

```bash
# Clone a PR or issue (to `gh/123` by default)
ghpr clone https://github.com/owner/repo/pull/123
# or
ghpr clone owner/repo#123
# or, from inside a repo on a branch with an open PR, no args:
ghpr clone

# Make edits to:
# - Title / Description: `gh/123/repo#123.md`
# - Comment files: `zNNNNNN-<author>.md` (existing comments) or `new.md` (new comments)

# Show differences (between local "clone" and GitHub)
ghpr diff

# Push changes
ghpr push
```

### Syncing: `fetch`, `pull`, `push`, `sync`

Each clone is its own git repo, and `refs/remotes/github` tracks GitHub's state within it — the analog of `origin/main`, reflog and all (`git branch -r` lists it as `github`). That makes the sync commands mirror their git namesakes:

```bash
ghpr fetch          # snapshot GitHub into the `github` ref; touches nothing else
ghpr fetch -n       # show what would come in, without moving the ref
ghpr pull           # fetch, then replay your local commits onto it
ghpr push           # send committed local state to GitHub + gist
ghpr sync           # pull + push: the full round trip
```

Like git's, `ghpr pull` does not write to the remote — `push` is the only verb that does. `ghpr sync` is the round trip when you want it.

`ghpr fetch` never touches your working tree, index, or branch, so you're free to reconcile however you like:

```bash
ghpr fetch
git diff HEAD github                # what changed on GitHub
git log github                      # every state GitHub has been in
git rebase --onto github <previous-ref-sha>
```

`ghpr pull` does that reconcile for you, defaulting to rebase:

```bash
ghpr pull                 # rebase: replay local commits onto the fetched state
ghpr pull -m merge        # merge commit instead
ghpr pull -m overwrite    # discard local commits; remote wins
git config ghpr.pullMode merge    # change the default
```

Both directions operate on **committed** state:

- `push` sends HEAD, never the working tree, so commit before pushing (dirty files are listed and skipped).
- `pull` refuses to rebase over uncommitted changes rather than overwriting them. `github` is still advanced, so you can commit and re-run, or reconcile by hand.
- `push` only advances `github` when the sync was complete; if anything was held back (uncommitted files, others' comments, `--no-comments`), the base stays put so the next `pull` still replays your work.
- `push` fetches first and **refuses** unless HEAD already contains GitHub's current state — git's non-fast-forward rule. `ghpr pull` (any mode) makes it an ancestor and clears the gate; `ghpr push -G` overrides. (Bootstrap pushes are ungated — see below.)

Repos cloned before the ref existed have no recorded base. On first use ghpr fetches and compares: if HEAD already matches GitHub the base is adopted silently, but if they differ there is no way to tell which side moved, and guessing loses data either way — so it refuses and asks you to decide:

```bash
ghpr pull -m overwrite              # remote wins, discard the local delta
ghpr push                           # local wins, send HEAD to GitHub
git update-ref refs/remotes/github <sha>          # or set the base by hand
```

Repos carrying an older ref name (`refs/remotes/github/remote`, `refs/ghpr/remote`) are migrated to `refs/remotes/github` automatically.

### Adding Comments

To add a new comment, create a file starting with `new` and ending in `.md`:

```bash
# Create a draft comment
echo "My comment text" > new.md

# Commit it
git add new.md
git commit -m "Draft comment"

# Push to GitHub (posts the comment and renames to z{id}-{author}.md)
ghpr push
```

The `push` command will:
1. Post `new*.md` files as comments to GitHub
2. Create a commit renaming them to `z{comment_id}-{author}.md`
3. Sync to the gist mirror

### Uploading Images

```bash
# Upload image(s) to this issue or PR's Gist mirror, and get markdown URLs
ghpr upload screenshot.png
# Output: ![screenshot.png](https://gist.githubusercontent.com/...)
```

**Note:** GitHub serves gist raw files as `application/octet-stream`, so images render in markdown but videos won't preview inline. For videos, use GitHub's native drag-drop upload in the web UI instead.

## Directory Structure

Cloned PRs and issues are stored as:
```
gh/
  123/                    # Filed PRs/issues (numbered)
    repo#123.md           # Main description
    z3404494861-user.md   # Comments (ID-author format)
    z3407382913-user.md
  drafts/                 # In-flight drafts (not yet filed)
    my-feature/
      DESCRIPTION.md
  new/                    # Default single-draft slot (ghpr init with no arg)
    DESCRIPTION.md
```

Since PRs are issues in GitHub's API, we use the same `gh/{number}/` pattern for both. Each `gh/<number>/` and `gh/drafts/<slug>/` is its own nested git repo, so the parent project can safely add `gh/` to its `.gitignore` — `ghpr clone` and `ghpr create` set up nested git tracking automatically.

### Parallel drafts

Pass a slug to stage multiple drafts at once:

```bash
ghpr init my-feature       # creates gh/drafts/my-feature/
ghpr init another-issue    # creates gh/drafts/another-issue/
ghpr create my-feature     # files the my-feature draft → renames to gh/<N>/
```

## Shell Integration (Optional)

For users who want shorter aliases, `ghpr` provides shell integration:

### Bash/Zsh

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
eval "$(ghpr shell-integration bash)"
```

### Fish

Add to your `~/.config/fish/config.fish`:

```fish
ghpr shell-integration fish | source
```

### Available Aliases

After enabling shell integration, you get convenient shortcuts and tab completion for subcommands, flags, and options:

```bash
ghprc      # ghpr clone (no args = clone branch's PR; + cd into directory)
ghpri      # ghpr init (+ cd into draft dir; pass slug for gh/drafts/<slug>/)
ghprcr     # ghpr create
ghprd      # ghpr diff
ghprp      # ghpr push
ghprl      # ghpr pull (fetch + reconcile; does not write to GitHub)
ghprs      # ghpr sync (pull + push)
ghprf      # ghpr fetch
ghpro      # ghpr open
ghprsh     # ghpr show
ghpru      # ghpr upload
ghia       # ghpr ingest-attachments
# ... and more (-n, -g, -o variants)
```

See the full list with:
```bash
ghpr shell-integration bash
```

[marin#1773]: https://github.com/marin-community/marin/issues/1773
[1773 comment]: https://github.com/marin-community/marin/issues/1773#issuecomment-3478991552
[1773 gist]: https://gist.github.com/ryan-williams/857fcaa8b2f80a250a70ac0250634ee5
[marin#1723]: https://github.com/marin-community/marin/pull/1723
[1723 gist]: https://gist.github.com/f38c0ab59897cfb57c99081b7d87af54
