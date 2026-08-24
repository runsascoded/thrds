"""Ingest attachments command - download user-attachments and convert to gist permalinks."""

import re
import subprocess
from os import environ
from utz import proc, err
from utz.cli import opt, flag

from ..files import find_description_file


def ingest_attachments(
    branch: str | None,
    no_ingest: bool,
    dry_run: bool,
) -> None:
    """Download GitHub user-attachments and convert to gist permalinks.

    This command:
    1. Finds reference-style links with user-attachments URLs in DESCRIPTION.md
    2. Downloads the attachments via gh api
    3. Commits them to a dedicated branch on the gist
    4. Replaces URLs with gist permalinks in the main branch
    """

    # Use provided branch, or fall back to env var, or default
    if not branch:
        branch = environ.get('GHPR_INGEST_BRANCH', 'attachments')

    # Check environment variable for default behavior
    should_ingest = not no_ingest
    if not no_ingest and environ.get('GHPR_INGEST_ATTACHMENTS') == '0':
        should_ingest = False

    if not should_ingest:
        err("Attachment ingestion disabled")
        return

    # Get PR info from current directory
    owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None)
    repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None)
    pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None)
    gist_id = proc.line('git', 'config', 'pr.gist', err_ok=True, log=None)

    if not all([owner, repo, pr_number]):
        err("Error: Not in a PR clone directory (missing pr.* git config)")
        exit(1)

    if not gist_id:
        err("Error: No gist associated with this PR clone")
        exit(1)

    # Find description file
    desc_file = find_description_file()
    if not desc_file:
        err("Error: No description file found")
        exit(1)

    # Read description content
    with open(desc_file, 'r') as f:
        content = f.read()

    # Pattern for reference-style links: [name]: url
    ref_link_pattern = re.compile(r'^\[([^\]]+)\]:\s+(https://github\.com/user-attachments/assets/[a-f0-9-]+)\s*$', re.MULTILINE)

    matches = ref_link_pattern.findall(content)
    if not matches:
        err("No user-attachments found in reference-style links")
        return

    err(f"Found {len(matches)} user-attachment(s) to process")

    # Store current branch
    current_branch = proc.line('git', 'rev-parse', '--abbrev-ref', 'HEAD', log=None)

    # Check if attachments branch exists
    branches = proc.lines('git', 'branch', '-a', log=None) or []
    branch_exists = any(b.strip().endswith(branch) for b in branches)

    if not branch_exists:
        if dry_run:
            err(f"[DRY-RUN] Would create branch '{branch}'")
        else:
            # Create orphan branch for attachments
            proc.run('git', 'checkout', '--orphan', branch, log=None)
            # Remove all files from index (might fail if working tree is empty, that's ok)
            try:
                proc.run('git', 'rm', '-rf', '.', log=None)
            except Exception:
                pass  # Empty working tree is fine
            # Create initial commit
            proc.run('git', 'commit', '--allow-empty', '-m', 'Initial commit for attachments', log=None)
            err(f"Created branch '{branch}' for attachments")
    else:
        if dry_run:
            err(f"[DRY-RUN] Would switch to branch '{branch}'")
        else:
            proc.run('git', 'checkout', branch, log=None)

    # Process each attachment
    replacements = []
    for name, url in matches:
        # Extract asset ID from URL
        asset_id = url.split('/')[-1]

        if dry_run:
            err(f"[DRY-RUN] Would download: {name} from {url}")
        else:
            err(f"Downloading: {name} from {url}")

            # Download via gh api
            try:
                # gh api returns binary data for these URLs
                result = subprocess.run(['gh', 'api', url, '--method', 'GET'],
                                       capture_output=True, check=False)
                if result.returncode != 0:
                    err(f"Failed to download {url}")
                    continue

                data = result.stdout

                # Try to determine file extension from content
                # Common magic bytes
                ext = '.bin'
                if data.startswith(b'\x89PNG'):
                    ext = '.png'
                elif data.startswith(b'\xff\xd8\xff'):
                    ext = '.jpg'
                elif data.startswith(b'GIF8'):
                    ext = '.gif'
                elif data.startswith(b'%PDF'):
                    ext = '.pdf'
                elif data.startswith(b'PK\x03\x04'):
                    ext = '.zip'

                # Use asset_id as filename with detected extension
                filename = f"{asset_id}{ext}"

                # Write file
                with open(filename, 'wb') as f:
                    f.write(data)

                # Add and commit
                proc.run('git', 'add', filename, log=None)
                proc.run('git', 'commit', '-m', f'Add {name}: {filename}', log=None)

                err(f"Saved as: {filename}")

                # Get the blob SHA for permalink
                blob_sha = proc.line('git', 'rev-parse', f'HEAD:{filename}', log=None)

                # Get GitHub username from gist metadata via gh api
                github_username = None
                try:
                    github_username = proc.line('gh', 'api', f'/gists/{gist_id}', '--jq', '.owner.login', log=None)
                except Exception as e:
                    err(f"Warning: Could not get gist owner: {e}")

                if not github_username:
                    err(f"Error: Could not determine GitHub username for gist {gist_id}")
                    err("The gist permalink requires the owner's username")
                    exit(1)

                # Build gist permalink with username for githubusercontent.com format
                gist_url = f"https://gist.githubusercontent.com/{github_username}/{gist_id}/raw/{blob_sha}/{filename}"

                replacements.append((name, url, gist_url))

            except Exception as e:
                err(f"Error downloading {url}: {e}")
                continue

    if not dry_run and replacements:
        # Push attachments branch
        err(f"Pushing {branch} branch to gist...")
        proc.run('git', 'push', '-u', 'g', branch, log=None)

        # Switch back to main branch
        proc.run('git', 'checkout', current_branch, log=None)

        # Update description file with new URLs
        new_content = content
        for name, old_url, new_url in replacements:
            pattern = f"[{name}]: {old_url}"
            replacement = f"[{name}]: {new_url}"
            new_content = new_content.replace(pattern, replacement)

        # Write updated content
        with open(desc_file, 'w') as f:
            f.write(new_content)

        # Commit the URL updates
        proc.run('git', 'add', str(desc_file), log=None)
        commit_msg = f"Replace user-attachments with gist permalinks\n\nConverted {len(replacements)} attachment(s) to gist permalinks"
        proc.run('git', 'commit', '-m', commit_msg, log=None)

        err(f"Updated {len(replacements)} reference(s) in {desc_file}")
        err("You can now push these changes with 'ghpr push' to update the PR")

    elif dry_run:
        err(f"[DRY-RUN] Would update {len(replacements)} reference(s)")


def register(cli):
    """Register command with CLI."""

    @cli.command(name='ingest-attachments')
    @flag('-n', '--dry-run', help='Show what would be done without making changes')
    @flag('--no-ingest', help='Disable attachment ingestion')
    @opt('-b', '--branch', help='Branch name for attachments (default: $GHPR_INGEST_BRANCH or "attachments")')
    def ingest_attachments_cmd(dry_run, no_ingest, branch):
        """Download GitHub user-attachments and convert to gist URLs."""
        ingest_attachments(branch, no_ingest, dry_run)
