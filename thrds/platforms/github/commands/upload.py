"""Upload command - upload files to gist."""

from pathlib import Path
from click import Choice
from utz import proc, err
from utz.cli import arg, opt

from ..config import get_pr_info_from_path


def upload(
    files: tuple[str, ...],
    branch: str,
    format: str,
    alt: str | None,
) -> None:
    """Upload images to the PR's gist and get URLs."""
    from utz.git.gist import upload_files_to_gist, format_upload_output

    # Get PR info
    owner, repo, pr_number = get_pr_info_from_path()

    if not all([owner, repo, pr_number]):
        try:
            owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None) or ''
            repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None) or ''
            pr_number = proc.line('git', 'config', 'pr.number', err_ok=True, log=None) or ''
        except Exception as e:
            err(f"Error: Could not determine PR from directory or git config: {e}")
            exit(1)

    # Get or create gist
    # Read gist ID from git config (optional)
    gist_id = proc.line('git', 'config', 'pr.gist', err_ok=True, log=None)

    if not gist_id:
        # Create a minimal gist for uploads
        err("Creating gist for PR assets...")
        from utz.git.gist import create_gist
        gist_id = create_gist(
            description=f'{owner}/{repo}#{pr_number} assets',
            content="# PR Assets\nImage assets for PR\n"
        )
        if gist_id:
            # Store the gist ID in git config
            proc.run('git', 'config', 'pr.gist', gist_id, log=None)
            err(f"Created gist: {gist_id}")
        else:
            err("Error: Failed to create gist")
            exit(1)

    # Check if we're already in a gist clone
    is_local_clone = False
    remote_name = None
    try:
        # Check all remotes to see if any point to this gist
        remotes = proc.lines('git', 'remote', log=None)
        for remote in remotes:
            if not remote:
                continue
            try:
                remote_url = proc.line('git', 'remote', 'get-url', remote, err_ok=True, log=None)
                if f'gist.github.com:{gist_id}' in remote_url or f'gist.github.com/{gist_id}' in remote_url:
                    is_local_clone = True
                    remote_name = remote
                    err(f"Already in gist repository with remote '{remote}'")
                    break
            except Exception:
                # Remote URL doesn't match gist pattern, continue checking others
                continue
    except Exception:
        # Not in a gist repo, which is expected for PR directories
        pass

    # Prepare files for upload
    file_list = []
    for file_path in files:
        filename = Path(file_path).name
        file_list.append((file_path, filename))

    # Upload files using the library
    results = upload_files_to_gist(
        file_list,
        gist_id,
        branch=branch,
        is_local_clone=is_local_clone,
        commit_msg=f'Add assets for {owner}/{repo}#{pr_number}',
        remote_name=remote_name
    )

    # Output formatted results
    for orig_name, safe_name, url in results:
        output = format_upload_output(orig_name, url, format, alt)
        print(output)

    if not results:
        exit(1)


def register(cli):
    """Register command with CLI."""

    @cli.command()
    @opt('-a', '--alt', help='Alt text for markdown/img format')
    @opt('-f', '--format', type=Choice(['url', 'markdown', 'img', 'auto']), default='auto', help='Output format (default: auto - img for images, url for others)')
    @opt('-b', '--branch', default='assets', help='Branch name in gist (default: assets)')
    @arg('files', nargs=-1, required=True)
    def upload_cmd(alt, format, branch, files):
        """Upload files to gist and output URLs."""
        upload(files, branch, format, alt)
