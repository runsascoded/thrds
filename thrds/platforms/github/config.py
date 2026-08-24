"""Git config helpers for storing and retrieving PR/Issue metadata."""

from pathlib import Path
from os import chdir

from utz import proc, err

from .patterns import GITHUB_URL_PATTERN, PR_DIR_PATTERN, GH_DIR_PATTERN, PR_INLINE_LINK_PATTERN


def get_pr_info_from_path(path: Path | None = None) -> tuple[str | None, str | None, str | None]:
    """Extract PR info from directory structure or git config."""
    if path is None:
        path = Path.cwd()
    else:
        path = Path(path)

    # First, check if we have PR info in git config (highest priority)
    owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None)
    repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None)
    pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None)
    if owner and repo and pr_number:
        return owner, repo, pr_number

    # Look for patterns in current or parent directories
    # Supports: pr<number>, issue<number>, gh<number> (legacy), or gh/<number> (new)
    current = path
    pr_number = None
    repo_path = None

    while current != current.parent:
        # Check if this dir matches pr<number>, issue<number>, or gh<number>
        match = PR_DIR_PATTERN.match(current.name)
        if match:
            pr_number = match.group(1)
            repo_path = current.parent
            break

        # Check for new gh/{number} structure
        if current.parent.name == 'gh' or GH_DIR_PATTERN.match(current.parent.name):
            if current.name.isdigit():
                pr_number = current.name
                repo_path = current.parent.parent
                break

        current = current.parent

    if not pr_number:
        # Check if we're in a directory with DESCRIPTION.md that has metadata
        desc_file = path / 'DESCRIPTION.md'
        if desc_file.exists():
            with open(desc_file, 'r') as f:
                first_line = f.readline().strip()
                # Look for pattern like # [owner/repo#123] or # [owner/repo#123](url)
                match = PR_INLINE_LINK_PATTERN.match(first_line)
                if match:
                    return match.group(1), match.group(2), match.group(3)

        err("Error: Could not determine PR number from directory structure")
        err("Expected to be in a directory named 'gh/{number}', 'pr<number>', 'issue<number>', or have DESCRIPTION.md with PR metadata")
        return None, None, None

    # Get repo info from parent directory
    chdir(repo_path)

    # Try to get owner/repo from git remote
    try:
        # Get the default remote
        remotes = proc.lines('git', 'remote', err_ok=True, log=None) or []

        for remote in ['origin', 'upstream'] + remotes:
            if not remote:
                continue
            try:
                url = proc.line('git', 'remote', 'get-url', remote, err_ok=True, log=None) or ''
                # Match GitHub URLs
                match = GITHUB_URL_PATTERN.search(url)
                if match:
                    owner = match.group(1)
                    repo = match.group(2)
                    return owner, repo, pr_number
            except Exception as e:
                # Log but continue checking other remotes
                err(f"Warning: Could not get URL for remote {remote}: {e}")
                continue
    except Exception as e:
        err(f"Error while checking git remotes: {e}")
        raise

    err("Error: Could not determine repository from git remotes")
    return None, None, pr_number
