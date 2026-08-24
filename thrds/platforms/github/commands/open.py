"""Open command - open PR or gist in web browser."""

import webbrowser
from utz import proc, err
from utz.cli import flag

from ..config import get_pr_info_from_path
from ..files import find_description_file
from ..gist import find_gist_remote
from ..patterns import GIST_ID_PATTERN, PR_FILENAME_PATTERN


def open_pr(gist: bool) -> None:
    """Open PR or gist in web browser."""
    # Get PR info
    owner, repo, pr_number = get_pr_info_from_path()

    if not all([owner, repo, pr_number]):
        # Try from git config
        owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''
        repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None) or ''
        pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None) or ''

    if not all([owner, repo, pr_number]):
        # Check for PR-specific files
        desc_file = find_description_file()
        if desc_file:
            # Parse PR info from the file
            match = PR_FILENAME_PATTERN.match(desc_file.name)
            if match:
                repo = match.group(1)
                pr_number = match.group(2)
                # Try to get owner from git config
                owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''

    if gist:
        # Open gist
        gist_id = proc.line('git', 'config', 'pr.gist', err_ok=True, log=None)
        if not gist_id:
            # Try to find from remote
            gist_remote = find_gist_remote()
            if gist_remote:
                remotes = proc.lines('git', 'remote', '-v', log=None)
                for remote_line in remotes:
                    if remote_line.startswith(f"{gist_remote}\t") and 'gist.github.com' in remote_line:
                        match = GIST_ID_PATTERN.search(remote_line)
                        if match:
                            gist_id = match.group(1)
                            break

        if gist_id:
            gist_url = f"https://gist.github.com/{gist_id}"
            webbrowser.open(gist_url)
            err(f"Opened: {gist_url}")
        else:
            err("No gist found for this PR")
            exit(1)
    else:
        # Open PR
        if all([owner, repo, pr_number]):
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            webbrowser.open(pr_url)
            err(f"Opened: {pr_url}")
        else:
            err("Error: No PR found in current directory")
            exit(1)


def register(cli):
    """Register command with CLI."""

    @cli.command(name='open')
    @flag('-g', '--gist', help='Open gist instead of PR')
    def open_cmd(gist):
        """Open PR/Issue or gist in browser."""
        open_pr(gist)
