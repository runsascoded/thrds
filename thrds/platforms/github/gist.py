"""Gist operations for creating and syncing GitHub gists."""

import re
from pathlib import Path

from utz import proc, err

from .patterns import (
    GIST_FOOTER_VISIBLE_PATTERN,
    GIST_FOOTER_HIDDEN_PATTERN,
    GIST_URL_WITH_USER_PATTERN,
)

# Constants
DEFAULT_GIST_REMOTE = 'g'


def create_gist(
    file_path: str | Path,
    description: str,
    is_public: bool = False,
    store_id: bool = True,
) -> str:
    """Create a GitHub gist and optionally store its ID in git config.

    Args:
        file_path: Path to the file to upload
        description: Description for the gist
        is_public: Whether the gist should be public (default: secret/unlisted)
        store_id: Whether to store the gist ID in git config (default: True)

    Returns:
        Gist ID
    """
    # Create gist using gh CLI
    cmd = [
        'gh', 'gist', 'create',
        '--desc', description,
        '--public' if is_public else None,
        file_path
    ]
    result_str = proc.text(*cmd, log=None)
    # Extract gist ID from URL (last part of the path)
    gist_url = result_str.strip()
    gist_id = gist_url.split('/')[-1]

    if store_id:
        # Store gist ID in git config
        proc.run('git', 'config', 'pr.gist', gist_id, log=None)

    err(f"Created gist: {gist_url}")
    return gist_id


def find_gist_remote() -> str | None:
    """Find the gist remote intelligently.

    Returns:
        Remote name if found, None otherwise

    Priority:
    1. Check git config pr.gist-remote
    2. Look for any remote pointing to gist.github.com
    3. If only one remote exists and it's a gist, use it
    4. Fall back to DEFAULT_GIST_REMOTE if it exists
    """
    # First check config
    configured = proc.line('git', 'config', 'pr.gist-remote', err_ok=True)
    if configured:
        return configured

    # Get all remotes
    remotes = proc.lines('git', 'remote', '-v', err_ok=True) or []
    if not remotes:
        return None

    # Parse remotes to find gist URLs
    gist_remotes = []
    all_remotes = {}
    for line in remotes:
        if '\t' in line:
            name, url = line.split('\t', 1)
            if 'gist.github.com' in url:
                if name not in gist_remotes:
                    gist_remotes.append(name)
            all_remotes[name] = url

    # If we found exactly one gist remote, use it
    if len(gist_remotes) == 1:
        return gist_remotes[0]

    # If multiple gist remotes, prefer DEFAULT_GIST_REMOTE if it's one of them
    if DEFAULT_GIST_REMOTE in gist_remotes:
        return DEFAULT_GIST_REMOTE

    # If we have any gist remote, use the first one
    if gist_remotes:
        return gist_remotes[0]

    # Check if DEFAULT_GIST_REMOTE exists even if not a gist URL
    if DEFAULT_GIST_REMOTE in all_remotes:
        return DEFAULT_GIST_REMOTE

    return None


def extract_gist_footer(body: str | None) -> tuple[str | None, str | None]:
    """Extract gist footer from body and return (body_without_footer, gist_url)."""

    if not body:
        return body, None

    lines = body.split('\n')

    # Check for visible footer format
    # Be permissive: allow optional blank line before "Synced with..."
    # Formats accepted:
    #   - <empty>\n---\nSynced with... (3 lines)
    #   - <empty>\n---\n<empty>\nSynced with... (4 lines)
    if len(lines) >= 3 and 'Synced with [gist](' in lines[-1]:
        # Check for format with extra blank line before "Synced with..."
        if len(lines) >= 4 and lines[-2].strip() == '' and lines[-3].strip() == '---' and lines[-4].strip() == '':
            match = GIST_FOOTER_VISIBLE_PATTERN.search(lines[-1])
            if match:
                gist_url = match.group(1)
                # Remove the footer (last 4 lines)
                body_without_footer = '\n'.join(lines[:-4]).rstrip()
                return body_without_footer, gist_url
        # Check for standard format without extra blank line
        elif lines[-2].strip() == '---' and lines[-3].strip() == '':
            match = GIST_FOOTER_VISIBLE_PATTERN.search(lines[-1])
            if match:
                gist_url = match.group(1)
                # Remove the footer (last 3 lines)
                body_without_footer = '\n'.join(lines[:-3]).rstrip()
                return body_without_footer, gist_url

    # Check if last line is a hidden gist footer (handle both old and new formats)
    if lines and lines[-1].strip().startswith('<!-- Synced with '):
        # Try new format with attribution (with or without revision)
        match = re.match(r'<!-- Synced with (https://gist\.github\.com/[a-f0-9]+(?:/[a-f0-9]+)?) via \[github-pr\.py\].*-->', lines[-1].strip())
        if not match:
            # Try old format without attribution (with or without revision)
            match = GIST_FOOTER_HIDDEN_PATTERN.match(lines[-1].strip())
        if match:
            gist_url = match.group(1)
            # Remove the footer line
            body_without_footer = '\n'.join(lines[:-1]).rstrip()
            return body_without_footer, gist_url

    return body, None


def add_gist_footer(
    body: str | None,
    gist_url: str,
    visible: bool = False,
) -> str:
    """Add or update gist footer in body."""
    body_without_footer, _ = extract_gist_footer(body)

    if visible:
        # Extract gist ID and revision from URL if available
        # URL format: https://gist.github.com/user/gist_id or https://gist.github.com/gist_id/revision
        gist_match = GIST_URL_WITH_USER_PATTERN.search(gist_url)
        if gist_match:
            gist_id = gist_match.group(1)
            revision = gist_match.group(2)
            if revision:
                gist_link = f'https://gist.github.com/{gist_id}/{revision}'
            else:
                gist_link = f'https://gist.github.com/{gist_id}'
        else:
            gist_link = gist_url

        footer = f'\n---\n\nSynced with [gist]({gist_link}) via [ghpr](https://github.com/runsascoded/ghpr)'
    else:
        footer = f'<!-- Synced with {gist_url} via [ghpr](https://github.com/runsascoded/ghpr) -->'

    if body_without_footer:
        return f'{body_without_footer}\n\n{footer}'
    else:
        return footer


def sync_to_gist(
    owner: str,
    repo: str,
    pr_number: str,
    content: str,
    return_url: bool = False,
    add_remote: bool = True,
    gist_private: bool = None,
) -> str | None:
    """Sync PR description to a gist.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        content: Content to sync to gist
        return_url: If True, return the gist URL with revision instead of None
        add_remote: If True, add gist as a git remote
        gist_private: If True, create private gist; if False, create public; if None, match repo visibility

    Returns:
        None or gist URL with revision if return_url=True
    """

    # Check if we already have a gist ID stored
    gist_id = proc.line('git', 'config', 'pr.gist', err_ok=True, log=None)

    # Find the gist remote intelligently
    gist_remote = find_gist_remote()
    if not gist_remote:
        # Only set a default if we're actually going to use it
        if gist_id or add_remote:
            gist_remote = DEFAULT_GIST_REMOTE
            err(f"No gist remote found, will use '{gist_remote}'")

    # Determine gist visibility
    if gist_private is not None:
        # Explicit visibility specified
        is_public = not gist_private  # Invert: if private flag is True, public is False
        err(f"Using explicit gist visibility: {'PUBLIC' if is_public else 'PRIVATE'}")
    else:
        # Check repository visibility to determine gist visibility
        try:
            repo_data = proc.json('gh', 'repo', 'view', f'{owner}/{repo}', '--json', 'visibility', err_ok=True, log=None) or {}
            is_public = repo_data.get('visibility', 'PUBLIC').upper() == 'PUBLIC'
            err(f"Repository visibility: {'PUBLIC' if is_public else 'PRIVATE'}, gist will match")
        except Exception as e:
            err(f"Error: Could not determine repository visibility: {e}")
            raise

    # Use PR-specific filename for better gist organization
    pr_filename = f'{repo}#{pr_number}.md'
    local_filename = pr_filename  # Use same filename locally
    description = f'{owner}/{repo}#{pr_number} - 2-way sync via ghpr (runsascoded/ghpr)'
    gist_url = None

    if gist_id:
        # Update existing gist
        err(f"Updating gist {gist_id}...")

        # Check if we need to rename the file in the gist
        try:
            gist_files = proc.json('gh', 'api', f'gists/{gist_id}', '--jq', '.files', log=None) or {}
            old_filename = None

            # Find the existing markdown file (could be DESCRIPTION.md or a PR-specific name)
            for fname in gist_files.keys():
                if fname.endswith('.md'):
                    old_filename = fname
                    break

            # Update gist with new filename if needed
            if old_filename and old_filename != pr_filename:
                # Rename file by deleting old and adding new
                proc.run('gh', 'api', f'gists/{gist_id}', '-X', 'PATCH',
                           '-f', f'description={description}',
                           '-f', f'files[{old_filename}][filename]={pr_filename}', log=None)
                err(f"Renamed gist file from {old_filename} to {pr_filename}")
            else:
                # Just update description
                proc.run('gh', 'api', f'gists/{gist_id}', '-X', 'PATCH',
                           '-f', f'description={description}', log=None)
        except Exception as e:
            err(f"Error: Could not update gist metadata: {e}")
            raise

        # Check if remote exists and push to it
        if add_remote:
            try:
                # Commit any changes and push to gist
                proc.run('git', 'add', local_filename, log=None)
                proc.run('git', 'commit', '-m', f'Update PR description for {owner}/{repo}#{pr_number}', log=None)
            except Exception:
                # May already be committed - check if there are changes
                if proc.line('git', 'diff', '--cached', '--name-only', local_filename, err_ok=True, log=None):
                    raise  # Re-raise if there were actual changes that failed to commit

            try:
                proc.run('git', 'push', gist_remote, 'main', '--force', log=None)
                err(f"Pushed to gist remote '{gist_remote}'")

                # Get the latest revision ID
                gist_data = proc.json('gh', 'api', f'gists/{gist_id}', log=None)
                history = gist_data.get('history', [])
                if history:
                    revision = history[0]['version']
                    gist_url = f'https://gist.github.com/{gist_id}/{revision}'
                else:
                    gist_url = f'https://gist.github.com/{gist_id}'

            except Exception as e:
                err(f"Error: Could not push to gist remote: {e}")
                raise
        else:
            # Not using remote, get current gist URL
            gist_url = f'https://gist.github.com/{gist_id}'

    else:
        # Create new gist
        err("Creating new gist...")

        # Write content to a temporary file
        with open(local_filename, 'w') as f:
            f.write(content)

        # Create gist
        gist_id = create_gist(local_filename, description, is_public=is_public, store_id=True)
        gist_url = f'https://gist.github.com/{gist_id}'

        # Initialize git repo and add remote if requested
        if add_remote:
            try:
                # Initialize git if not already done
                proc.run('git', 'init', log=None, err_ok=True)

                # Commit the file
                proc.run('git', 'add', local_filename, log=None)
                proc.run('git', 'commit', '-m', f'Initial PR description for {owner}/{repo}#{pr_number}', log=None)

                # Add remote and push
                proc.run('git', 'remote', 'add', gist_remote, f'https://gist.github.com/{gist_id}.git', log=None)
                proc.run('git', 'branch', '-M', 'main', log=None)
                proc.run('git', 'push', '-u', gist_remote, 'main', log=None)
                err(f"Added gist as remote '{gist_remote}' and pushed")

                # Store remote name in config
                proc.run('git', 'config', 'pr.gist-remote', gist_remote, log=None)

                # Get the revision ID after push
                gist_data = proc.json('gh', 'api', f'gists/{gist_id}', log=None)
                history = gist_data.get('history', [])
                if history:
                    revision = history[0]['version']
                    gist_url = f'https://gist.github.com/{gist_id}/{revision}'

            except Exception as e:
                err(f"Error: Could not set up git remote for gist: {e}")
                raise

    if return_url:
        return gist_url
    return None
