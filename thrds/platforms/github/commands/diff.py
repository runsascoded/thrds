"""Diff command - show differences between local and remote."""

import sys
from click import Choice
from utz import proc, err
from utz.cli import opt, flag

from ..api import get_item_metadata, get_current_github_user
from ..config import get_pr_info_from_path
from ..files import read_description_from_git
from ..gist import extract_gist_footer
from ..patterns import extract_title_from_first_line
from ..render import render_comment_diff, render_unified_diff


def diff(
    color: str,
    no_comments: bool,
) -> None:
    """Show differences between local and remote PR/Issue descriptions and comments."""

    # Determine if we should use color
    use_color = False
    if color == 'always':
        use_color = True
    elif color == 'auto':
        use_color = sys.stdout.isatty()

    # ANSI color codes
    RED = '\033[31m' if use_color else ''
    GREEN = '\033[32m' if use_color else ''
    CYAN = '\033[36m' if use_color else ''
    YELLOW = '\033[33m' if use_color else ''
    RESET = '\033[0m' if use_color else ''
    BOLD = '\033[1m' if use_color else ''

    # Get PR info from current directory
    owner, repo, pr_number = get_pr_info_from_path()

    if not all([owner, repo, pr_number]):
        # Try git config
        try:
            owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''
            repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None) or ''
            pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None) or ''
        except Exception as e:
            err(f"Error: Could not determine PR from directory or git config: {e}")
            exit(1)

    # Get item type
    item_type = proc.line('git', 'config', 'pr.type', err_ok=True, log=None)

    # Get remote PR/Issue data
    item_label = 'issue' if item_type == 'issue' else 'PR'
    err(f"Fetching {item_label} {owner}/{repo}#{pr_number}...")
    pr_data, item_type = get_item_metadata(owner, repo, pr_number, item_type)
    if not pr_data:
        exit(1)

    # Read local description from git
    desc_content, desc_file = read_description_from_git('HEAD')
    if not desc_content or not desc_file:
        err("Error: Could not read description file from HEAD")
        err("Make sure you've committed your changes")
        exit(1)

    # Parse local file to get title and body
    lines = desc_content.split('\n')
    if not lines:
        err("Error: DESCRIPTION.md is empty")
        exit(1)

    first_line = lines[0].strip()
    local_title = extract_title_from_first_line(first_line)

    body_lines = lines[1:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    local_body = '\n'.join(body_lines).rstrip()

    # Strip footer from local body for comparison
    local_body_without_footer, _ = extract_gist_footer(local_body)

    # Get remote title and body (already normalized in get_pr_metadata)
    remote_title = pr_data['title']
    remote_body = (pr_data['body'] or '').rstrip()

    # Strip footer from remote body for comparison
    remote_body_without_footer, _ = extract_gist_footer(remote_body)

    # Compare titles
    if local_title != remote_title:
        err(f"\n{BOLD}{YELLOW}=== Title Differences ==={RESET}")
        err(f"{GREEN}Local: {RESET} {local_title}")
        err(f"{RED}Remote:{RESET} {remote_title}")
    else:
        err(f"\n{BOLD}{CYAN}=== Title: No differences ==={RESET}")

    # Compare bodies (without footers)
    if local_body_without_footer != remote_body_without_footer:
        err(f"\n{BOLD}{YELLOW}=== Body Differences ==={RESET}")
        render_unified_diff(
            remote_body_without_footer,
            local_body_without_footer,
            fromfile='Remote PR',
            tofile=f'Local {desc_file.name}',
            use_color=use_color,
            log=print
        )
    else:
        err(f"\n{BOLD}{CYAN}=== Body: No differences ==={RESET}")

    # Handle comment diffing (default enabled, skip if --no-comments)
    if not no_comments:
        # Get item type
        item_type = proc.line('git', 'config', 'pr.type', err_ok=True, log=None)
        if not item_type:
            _, item_type = get_item_metadata(owner, repo, pr_number)

        current_user = get_current_github_user()
        render_comment_diff(owner, repo, pr_number, item_type, use_color=use_color, current_user=current_user)

        if item_type == 'pr':
            from .. import reviews
            reviews.diff(owner, repo, pr_number, use_color=use_color, current_user=current_user)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @flag('--no-comments', help='Skip diffing comments')
    @opt('-c', '--color', type=Choice(['auto', 'always', 'never']), default='auto', help='When to use colored output (default: auto)')
    def diff_cmd(no_comments, color):
        """Show differences between local and remote PR/Issue."""
        diff(color, no_comments)
