"""Rendering utilities for diffs and comments."""

import difflib
from glob import glob
from pathlib import Path
from utz import proc, err

from .api import get_item_comments
from .comments import read_comment_file, get_comment_id_from_filename


def link(url: str | None, use_color: bool = True) -> str:
    """Render a URL for terminal display (plain — terminals linkify it)."""
    if not url:
        return ''
    CYAN = '\033[36m' if use_color else ''
    RESET = '\033[0m' if use_color else ''
    return f'{CYAN}{url}{RESET}'


def render_comment_diff(
    owner: str,
    repo: str,
    number: str,
    item_type: str,
    use_color: bool = True,
    dry_run: bool = False,
    current_user: str | None = None,
) -> tuple[int, int]:
    """Render comment differences between local and remote.

    Returns:
        (drafts_count, changes_count): Number of draft comments and changed comments
    """
    # ANSI color codes
    RED = '\033[31m' if use_color else ''
    GREEN = '\033[32m' if use_color else ''
    CYAN = '\033[36m' if use_color else ''
    YELLOW = '\033[33m' if use_color else ''
    RESET = '\033[0m' if use_color else ''
    BOLD = '\033[1m' if use_color else ''

    # Check for draft comments (new*.md files) from HEAD
    try:
        head_files = proc.lines('git', 'ls-tree', '--name-only', 'HEAD', log=False)
        draft_files = [f for f in head_files if f.startswith('new') and f.endswith('.md')]
    except Exception:
        draft_files = []

    drafts_count = 0
    if draft_files:
        err(f"\n{BOLD}=== Draft comments to post ==={RESET}")
        for draft_file in draft_files:
            try:
                draft_content = proc.text('git', 'show', f'HEAD:{draft_file}', log=False)
                if not draft_content.strip():
                    continue

                drafts_count += 1
                err(f"\n{BOLD}New comment from {draft_file}:{RESET}")
                # Show preview in green (it's being added)
                lines = draft_content.strip().split('\n')
                preview = '\n'.join(lines[:10])
                if len(lines) > 10:
                    err(f"{GREEN}{preview}{RESET}")
                    err(f"{BOLD}... ({len(lines) - 10} more lines){RESET}")
                else:
                    err(f"{GREEN}{preview}{RESET}")
            except Exception as e:
                err(f"Warning: Could not read {draft_file}: {e}")

    # Get remote comments
    remote_comments = get_item_comments(owner, repo, number, item_type)
    remote_comments_by_id = {str(c['id']): c for c in remote_comments}

    # Find all local comment files
    comment_files = sorted(glob('z[0-9]*.md'))

    changes_count = 0
    others_with_diffs = []
    if comment_files:
        local_comment_ids = set()
        for comment_file_path in comment_files:
            comment_id = get_comment_id_from_filename(comment_file_path)
            if not comment_id:
                continue
            local_comment_ids.add(comment_id)

            author, created_at, updated_at, local_body = read_comment_file(Path(comment_file_path))

            if comment_id in remote_comments_by_id:
                # Compare with remote
                remote_comment = remote_comments_by_id[comment_id]
                remote_body = remote_comment.get('body', '').replace('\r\n', '\n')
                comment_url = remote_comment.get('html_url', f'Comment {comment_id}')

                if local_body != remote_body:
                    is_others = current_user and author != current_user
                    if is_others:
                        others_with_diffs.append(comment_file_path)
                    changes_count += 1
                    err(f"\n{BOLD}{YELLOW}Comment {comment_id} (by {author}) - Differences:{RESET} {link(comment_url, use_color)}")
                    if is_others:
                        err(f"{YELLOW}  ⚠ Not your comment; won't be pushed without `-C`{RESET}")
                    render_unified_diff(
                        remote_body,
                        local_body,
                        fromfile=comment_url,
                        tofile=comment_file_path,
                        use_color=use_color,
                        log=print
                    )
                else:
                    err(f"{CYAN}Comment {comment_id} (by {author}): No differences{RESET} {link(comment_url, use_color)}")
            else:
                err(f"{YELLOW}Comment {comment_id} exists locally but not remotely{RESET}")

        # Check for remote comments not present locally
        for remote_comment in remote_comments:
            comment_id = str(remote_comment['id'])
            if comment_id not in local_comment_ids:
                author = remote_comment.get('user', {}).get('login', 'unknown')
                err(f"{YELLOW}Comment {comment_id} (by {author}) exists remotely but not locally{RESET}")

    if others_with_diffs:
        n = len(others_with_diffs)
        err(f"\n{BOLD}{YELLOW}⚠ {n} comment(s) with local changes won't be pushed (not yours). Use `ghprp -C` to force.{RESET}")

    return drafts_count, changes_count


def render_unified_diff(
    remote_content: str,
    local_content: str,
    fromfile: str,
    tofile: str,
    use_color: bool = True,
    log=None,
) -> None:
    """Render a colored unified diff.

    Args:
        remote_content: Remote content to compare
        local_content: Local content to compare
        fromfile: Label for remote content
        tofile: Label for local content
        use_color: Whether to use ANSI color codes
        log: Function to use for output (default: err for stderr)
    """
    if log is None:
        log = err

    # ANSI color codes
    RED = '\033[31m' if use_color else ''
    GREEN = '\033[32m' if use_color else ''
    CYAN = '\033[36m' if use_color else ''
    BOLD = '\033[1m' if use_color else ''
    RESET = '\033[0m' if use_color else ''

    # Check if contents have final newlines
    remote_has_final_newline = remote_content.endswith('\n')
    local_has_final_newline = local_content.endswith('\n')

    # For proper diff display, normalize both to end with newline for comparison
    # This prevents difflib from showing the last line as changed when only the
    # trailing newline differs
    remote_normalized = remote_content if remote_has_final_newline else remote_content + '\n'
    local_normalized = local_content if local_has_final_newline else local_content + '\n'

    local_lines = local_normalized.splitlines(keepends=True)
    remote_lines = remote_normalized.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        remote_lines,
        local_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm=''
    ))

    # Only show diff if there are actual content differences
    if diff_lines:
        for line in diff_lines:
            line = line.rstrip('\n')
            if line.startswith('+++'):
                log(f"{BOLD}{line}{RESET}")
            elif line.startswith('---'):
                log(f"{BOLD}{line}{RESET}")
            elif line.startswith('@@'):
                log(f"{CYAN}{line}{RESET}")
            elif line.startswith('+'):
                log(f"{GREEN}{line}{RESET}")
            elif line.startswith('-'):
                log(f"{RED}{line}{RESET}")
            else:
                log(line)

        # Add git-style "No newline at end of file" indicator when sides differ
        if remote_has_final_newline != local_has_final_newline:
            log(f"{CYAN}\\ No newline at end of file{RESET}")
    elif remote_has_final_newline != local_has_final_newline:
        # Only difference is trailing newline - show minimal diff
        log(f"{BOLD}--- {fromfile}{RESET}")
        log(f"{BOLD}+++ {tofile}{RESET}")
        log(f"{CYAN}Only trailing newline differs{RESET}")
