"""Show command - display PR and gist URLs."""

from utz import proc, err
from utz.cli import flag

from ..config import get_pr_info_from_path
from ..gist import find_gist_remote
from ..patterns import GIST_ID_PATTERN


def show(gist: bool) -> None:
    """Show PR and/or gist URLs for current directory."""
    # Get PR info
    owner, repo, pr_number = get_pr_info_from_path()

    if not all([owner, repo, pr_number]):
        # Try from git config
        owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''
        repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None) or ''
        pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None) or ''

    if gist:
        # Only show gist URL
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
            print(f"https://gist.github.com/{gist_id}")
        else:
            err("No gist found for this PR")
            exit(1)
    else:
        # Show both PR and gist
        if all([owner, repo, pr_number]):
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            print(f"PR: {pr_url}")

            # Check for gist
            gist_id = proc.line('git', 'config', 'pr.gist', err_ok=True, log=None)
            if gist_id:
                gist_url = f"https://gist.github.com/{gist_id}"
                print(f"Gist: {gist_url}")

            # Check for gist remote if no gist ID in config
            if not gist_id:
                gist_remote = find_gist_remote()
                if gist_remote:
                    remotes = proc.lines('git', 'remote', '-v', log=None)
                    for remote_line in remotes:
                        if remote_line.startswith(f"{gist_remote}\t"):
                            if 'gist.github.com' in remote_line:
                                match = GIST_ID_PATTERN.search(remote_line)
                                if match:
                                    gist_url = f"https://gist.github.com/{match.group(1)}"
                                    print(f"Gist (from remote): {gist_url}")
                                    break
        else:
            err("No PR information found in current directory")
            exit(1)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @flag('-g', '--gist', help='Only show gist URL')
    def show_cmd(gist):
        """Show PR/Issue and gist URLs."""
        show(gist)
