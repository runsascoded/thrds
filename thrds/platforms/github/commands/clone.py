"""Clone command - clone PR/Issue to local directory."""

import re
import webbrowser
from os import chdir, unlink, environ
from os.path import exists
from pathlib import Path
from tempfile import NamedTemporaryFile
from click import Context
from utz import proc, err, cd
from utz.cli import opt, flag, arg

from ..api import get_item_metadata, get_item_comments
from ..comments import write_comment_file
from ..config import get_pr_info_from_path
from ..files import get_expected_description_filename, write_description_with_link_ref
from ..gist import extract_gist_footer, add_gist_footer, create_gist, DEFAULT_GIST_REMOTE, find_gist_remote
from ..patterns import parse_pr_spec, GIST_ID_PATTERN, GITHUB_ITEM_URL_PATTERN
from ..refs import set_remote_ref


def _detect_current_branch_pr() -> tuple[str | None, str | None, str | None, str | None]:
    """Try to find an open PR for the current branch.

    Uses `gh pr view` which finds the PR associated with the current branch.

    Returns:
        Tuple of (owner, repo, number, item_type) or (None, None, None, None).
    """
    from subprocess import DEVNULL
    try:
        data = proc.json(
            'gh', 'pr', 'view', '--json', 'number,url',
            log=None, err_ok=True, stderr=DEVNULL,
        )
        if data and data.get('number'):
            url = data.get('url', '')
            match = GITHUB_ITEM_URL_PATTERN.match(url)
            if match:
                owner, repo, _, number = match.groups()
                return owner, repo, number, 'pr'
            # Fallback: use repo view for owner/repo
            repo_data = proc.json('gh', 'repo', 'view', '--json', 'owner,name', log=None)
            return repo_data['owner']['login'], repo_data['name'], str(data['number']), 'pr'
    except Exception:
        pass
    return None, None, None, None


def clone(
    directory: str | None,
    no_gist: bool,
    no_comments: bool,
    spec: str | None,
) -> None:
    """Clone a PR or Issue description and comments to a local directory.

    SPEC can be:
    - A PR/Issue number (when run from within a repo)
    - owner/repo#number format
    - A full PR/Issue URL
    """

    # Parse spec
    if spec:
        # Try to parse different formats
        owner, repo, number, item_type = parse_pr_spec(spec)

        # If just a number, need to get owner/repo from current directory
        if number and not owner:
            # Get owner/repo from current directory
            try:
                repo_data = proc.json('gh', 'repo', 'view', '--json', 'owner,name', log=None)
                owner = repo_data['owner']['login']
                repo = repo_data['name']
            except Exception as e:
                err(f"Error: Could not determine repository: {e}")
                err("Use owner/repo#number format.")
                exit(1)
    else:
        # Try to detect PR for the current branch
        owner, repo, number, item_type = _detect_current_branch_pr()
        if not number:
            # Fall back to inferring from directory structure
            owner, repo, number = get_pr_info_from_path()
            item_type = None

    if not all([owner, repo, number]):
        err("Error: Could not determine PR/Issue to clone")
        err("Usage: ghpr clone [NUMBER | owner/repo#NUMBER | URL]")
        exit(1)

    # Get metadata and detect type
    err(f"Fetching {owner}/{repo}#{number}...")
    item_data, detected_type = get_item_metadata(owner, repo, number, item_type)
    if not item_data:
        exit(1)

    # Use gh/{number} naming (PRs are issues, so same pattern for both)
    if not directory:
        # Declarative goal: create at {git_root}/gh/{number}/
        # Calculate relative path from cwd to that target
        try:
            from utz.git import root as git_root
            root_path = Path(git_root())
            target_absolute = root_path / 'gh' / str(number)
            cwd = Path.cwd()
            directory = str(target_absolute.relative_to(cwd))
        except Exception:
            # Fallback: if not in a git repo or can't determine, use gh/{number}
            directory = f'gh/{number}'

    target_path = Path(directory)

    # Check if directory already exists
    if exists(target_path):
        err(f"Error: Directory {directory} already exists")
        exit(1)

    # Report what we found
    item_label = 'issue' if detected_type == 'issue' else 'PR'
    err(f"Found {item_label}: {item_data.get('title', 'No title')}")

    # Create directory and initialize git repo
    target_path.mkdir(parents=True)
    chdir(target_path)

    proc.run('git', 'init', '-q', log=None)

    # Create item-specific filename
    new_filename = get_expected_description_filename(owner, repo, number)
    desc_file = Path(new_filename)
    title = item_data['title']
    body = item_data['body'] or ''
    url = item_data['url']

    # Strip any gist footer from the body before saving locally
    body_without_footer, existing_gist_url = extract_gist_footer(body)

    # Write using helper to avoid duplicate link definitions
    write_description_with_link_ref(desc_file, owner, repo, number, title, body_without_footer, url)

    # Store metadata in git config
    proc.run('git', 'config', 'pr.owner', owner, log=None)
    proc.run('git', 'config', 'pr.repo', repo, log=None)
    proc.run('git', 'config', 'pr.number', str(number), log=None)
    proc.run('git', 'config', 'pr.url', item_data['url'], log=None)
    proc.run('git', 'config', 'pr.type', detected_type, log=None)

    # Initial commit
    item_label = 'issue' if detected_type == 'issue' else 'PR'
    proc.run('git', 'add', new_filename, log=None)
    proc.run('git', 'commit', '-m', f'Initial clone of {item_label} {owner}/{repo}#{number}', log=None)

    # Create or use existing gist unless --no-gist was specified
    gist_url = None
    if not no_gist:
        gist_id = None

        # Check if PR already has a gist in its footer
        if existing_gist_url:
            # Extract gist ID from the URL
            match = GIST_ID_PATTERN.search(existing_gist_url)
            if match:
                gist_id = match.group(1)
                gist_url = existing_gist_url
                err(f"Found existing gist in {item_label} description: {gist_url}")

                # Store gist ID
                proc.run('git', 'config', 'pr.gist', gist_id, log=None)

                # Add gist as remote
                proc.run('git', 'remote', 'add', DEFAULT_GIST_REMOTE, f'git@gist.github.com:{gist_id}.git', log=None)
                proc.run('git', 'config', 'pr.gist-remote', DEFAULT_GIST_REMOTE, log=None)

                # Fetch and push our version
                proc.run('git', 'fetch', DEFAULT_GIST_REMOTE, log=None)
                proc.run('git', 'push', '--set-upstream', DEFAULT_GIST_REMOTE, 'main', '--force', log=None)
                err("Pushed to existing gist")

        # Create new gist if none exists
        if not gist_id:
            err(f"Creating gist for {item_label} sync...")

            # Get repository visibility to determine gist visibility
            try:
                is_private = proc.json('gh', 'api', f'repos/{owner}/{repo}', '--jq', '.private', log=None)
                gist_private = is_private if isinstance(is_private, bool) else True
            except Exception as e:
                err(f"Error: Could not determine repo visibility: {e}")
                raise

            # Create the gist
            item_label_lower = item_label.lower()
            description = f'{owner}/{repo}#{number} ({item_label_lower}) - 2-way sync via ghpr (https://github.com/runsascoded/ghpr)'
            gist_id = create_gist(desc_file, description, is_public=not gist_private)

            # Add gist as remote
            proc.run('git', 'remote', 'add', DEFAULT_GIST_REMOTE, f'git@gist.github.com:{gist_id}.git', log=None)
            proc.run('git', 'config', 'pr.gist-remote', DEFAULT_GIST_REMOTE, log=None)

            # Fetch and push
            proc.run('git', 'fetch', DEFAULT_GIST_REMOTE, log=None)
            proc.run('git', 'push', '--set-upstream', DEFAULT_GIST_REMOTE, 'main', '--force', log=None)
            err("Pushed to gist")

            # Construct gist URL
            gist_url = f"https://gist.github.com/{gist_id}"

            # Only add gist footer if user is the author (avoid editing others' PRs/Issues)
            try:
                from ..api import get_current_github_user
                current_user = get_current_github_user()
                pr_author = item_data.get('user', {}).get('login')

                if current_user == pr_author:
                    err(f"Adding gist footer to {item_label}...")
                    body_with_footer = add_gist_footer(body, gist_url, visible=False)

                    # Update item with footer
                    with NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
                        f.write(body_with_footer)
                        temp_file = f.name

                    try:
                        gh_cmd = 'pr' if detected_type == 'pr' else 'issue'
                        proc.run('gh', gh_cmd, 'edit', str(number), '-R', f'{owner}/{repo}',
                                '--body-file', temp_file, log=None)
                        err(f"Added gist footer to {item_label}")
                    finally:
                        unlink(temp_file)
                else:
                    err(f"Skipping gist footer (not the {item_label} author)")
                    err(f"Gist URL: {gist_url}")
            except Exception as e:
                err(f"Warning: Could not add gist footer: {e}")
                err(f"Gist URL: {gist_url}")

    err(f"Successfully cloned {item_label} to {target_path}")
    err(f"URL: {item_data['url']}")

    # Output directory path to stdout for shell integration (with marker for reliable parsing)
    print(f"GHPR_DIR:{target_path}")

    # Fetch and store comments (default enabled, skip if --no-comments)
    if not no_comments:
        err(f"Fetching comments for {item_label}...")
        comments = get_item_comments(owner, repo, number, detected_type)
        if comments:
            err(f"Found {len(comments)} comment(s)")
            for comment in comments:
                comment_id = str(comment['id'])
                author = comment['user']['login']
                created_at = comment['created_at']
                updated_at = comment.get('updated_at')
                body = comment.get('body', '')

                # Write comment file
                comment_file = write_comment_file(comment_id, author, created_at, updated_at, body)
                proc.run('git', 'add', str(comment_file), log=None)

            # Commit all comments
            proc.run('git', 'commit', '-m', f'Add {len(comments)} comment(s)', log=None)
            err(f"Committed {len(comments)} comment(s)")

            # Push comments to gist if one was created
            if gist_url:
                gist_remote = find_gist_remote()
                if gist_remote:
                    proc.run('git', 'push', gist_remote, 'main', '--force', log=None)
                    err("Pushed comments to gist")
        else:
            # Inline review threads (if any) are fetched separately, just below.
            err("No top-level comments found" if detected_type == 'pr' else "No comments found")

    # Fetch review threads (PRs only)
    if not no_comments and detected_type == 'pr':
        from .. import reviews
        err("Fetching review threads...")
        n_threads, r_new, r_updated = reviews.pull(owner, repo, str(number))
        if r_new or r_updated:
            proc.run('git', 'commit', '-m',
                     f'Add {n_threads} review thread(s), {r_new} comment(s)', log=None)
            err(f"Committed {n_threads} review thread(s)")
            if gist_url:
                gist_remote = find_gist_remote()
                if gist_remote:
                    proc.run('git', 'push', gist_remote, 'main', '--force', log=None)
                    err("Pushed review threads to gist")

    # Everything committed so far mirrors GitHub, so it's the base future pulls
    # reconcile against (see ghpr/refs.py).
    set_remote_ref('HEAD')

    # Check if we should ingest user-attachments
    should_ingest = environ.get('GHPR_INGEST_ATTACHMENTS', '1') != '0'

    if should_ingest and gist_url:
        # Check for user-attachments in the cloned description
        desc_file = target_path / new_filename
        if desc_file.exists():
            with open(desc_file, 'r') as f:
                content = f.read()

            # Check for user-attachments in reference-style links
            ref_link_pattern = re.compile(r'^\[([^\]]+)\]:\s+(https://github\.com/user-attachments/assets/[a-f0-9-]+)\s*$', re.MULTILINE)
            if ref_link_pattern.search(content):
                err("Found user-attachments, ingesting...")
                # Change to the clone directory and run ingest
                with cd(target_path):
                    # Import here to avoid circular dependency
                    from . import ingest_attachments as ingest_module
                    # Call the ingest function directly
                    ctx = Context(ingest_module.ingest_attachments)
                    # Use None for branch to let it fall back to env var or default
                    ctx.invoke(ingest_module.ingest_attachments, branch=None, no_ingest=False, dry_run=False)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @arg('spec', required=False)
    @flag('--no-comments', help='Skip cloning comments')
    @flag('-G', '--no-gist', help='Skip creating a gist')
    @opt('-d', '--directory', help='Directory to clone into (default: gh/{number})')
    def clone_cmd(spec, no_comments, no_gist, directory):
        """Clone a PR or Issue description and comments to a local directory."""
        clone(directory, no_gist, no_comments, spec)
