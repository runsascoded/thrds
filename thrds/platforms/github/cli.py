"""CLI commands for ghpr."""

import re
import sys
import difflib
from functools import partial
from glob import glob
from os import chdir, environ, unlink
from os.path import abspath, dirname, exists, join
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
import webbrowser
import subprocess

from click import Choice, Context, group
from utz import proc, err, cd
from utz.cli import arg, flag, opt

from .api import get_item_metadata, get_pr_metadata, get_current_github_user, get_item_comments
from .comments import write_comment_file, read_comment_file, get_comment_id_from_filename
from .config import get_pr_info_from_path
from .files import (
    get_expected_description_filename,
    find_description_file,
    read_description_from_git,
    write_description_with_link_ref,
    read_description_file,
    upload_image_to_github,
    process_images_in_description,
)
from .gist import (
    create_gist,
    find_gist_remote,
    extract_gist_footer,
    add_gist_footer,
    DEFAULT_GIST_REMOTE,
)
from .patterns import (
    extract_title_from_first_line,
    parse_pr_spec,
    PR_LINK_IN_H1_PATTERN,
    PR_INLINE_LINK_PATTERN,
    PR_LINK_REF_PATTERN,
    PR_SPEC_PATTERN,
    PR_FILENAME_PATTERN,
    PR_DIR_PATTERN,
    GH_DIR_PATTERN,
    GIST_ID_PATTERN,
    GIST_URL_WITH_USER_PATTERN,
)


# Import resolve_remote_ref from utz
try:
    from utz.git.branch import resolve_remote_ref
except ImportError:
    # Fallback: define a stub that raises an informative error
    def resolve_remote_ref(verbose=False):
        raise ImportError(
            "utz.git.branch.resolve_remote_ref not available. Update utz or specify --head explicitly."
        )


def get_owner_repo(repo_arg: str | None = None) -> tuple[str, str]:
    """Get owner and repo, trying multiple sources.

    Tries in order:
    1. Explicit -r/--repo argument (owner/repo format)
    2. Git config (pr.owner, pr.repo)
    3. Parent directory's GitHub repo
    4. Current directory's git remotes (origin, then any)

    Returns:
        Tuple of (owner, repo)

    Raises:
        SystemExit if unable to determine repo
    """
    # Try explicit argument
    if repo_arg:
        if '/' in repo_arg:
            parts = repo_arg.split('/')
            if len(parts) == 2:
                return parts[0], parts[1]
        err(f"Error: Invalid repo format '{repo_arg}'. Use: owner/repo")
        exit(1)

    # Try git config
    owner = proc.line('git', 'config', 'pr.owner', err_ok=True, log=None)
    repo = proc.line('git', 'config', 'pr.repo', err_ok=True, log=None)
    if owner and repo:
        return owner, repo

    # Try walking up to find a git repo with GitHub remote
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / '.git').exists():
            try:
                with cd(parent):
                    repo_data = proc.json('gh', 'repo', 'view', '--json', 'owner,name', log=None, err_ok=True)
                    if repo_data:
                        return repo_data['owner']['login'], repo_data['name']
            except Exception:
                pass
            # Continue searching even if gh failed - might find a parent with GitHub remote

    # Try current directory's git remotes
    try:
        from utz.git.github import get_remotes
        github_remotes = get_remotes()
        if github_remotes:
            # Try 'origin' first, then 'u', then any
            for remote_name in ['origin', 'u'] + list(github_remotes.keys()):
                if remote_name in github_remotes:
                    owner_repo = github_remotes[remote_name]
                    parts = owner_repo.split('/')
                    if len(parts) == 2:
                        return parts[0], parts[1]
                    break
    except Exception:
        pass

    err("Error: Could not determine repository.")
    err("Specify with: -r owner/repo")
    err("Or configure with: ghpr init -r owner/repo")
    err("Or run from a directory with a GitHub remote")
    exit(1)


@group()
def cli():
    """Clone and sync GitHub PR descriptions."""
    pass


# Patch Click to suggest options on bare <tab> (not just after `-`).
# By default Click only suggests options when the user has typed `-`, and
# when a positional argument is pending it only completes that argument's
# type (which for plain strings returns nothing). This patch makes both
# Command.shell_complete and Argument.shell_complete include options.
def _patch_shell_complete():
    from click import Argument, Command, Option
    from click.core import ParameterSource
    from click.shell_completion import CompletionItem

    def _get_option_completions(cmd, ctx):
        results = []
        for param in cmd.get_params(ctx):
            if (
                not isinstance(param, Option)
                or param.hidden
                or (
                    not param.multiple
                    and ctx.get_parameter_source(param.name)
                    is ParameterSource.COMMANDLINE
                )
            ):
                continue
            results.extend(
                CompletionItem(name, help=param.help)
                for name in [*param.opts, *param.secondary_opts]
            )
        return results

    _orig_cmd = Command.shell_complete

    def _patched_cmd(self, ctx, incomplete):
        results = _orig_cmd(self, ctx, incomplete)
        if not incomplete:
            results.extend(_get_option_completions(self, ctx))
        return results

    Command.shell_complete = _patched_cmd

    _orig_arg = Argument.shell_complete

    def _patched_arg(self, ctx, incomplete):
        results = _orig_arg(self, ctx, incomplete)
        if not incomplete:
            results.extend(_get_option_completions(ctx.command, ctx))
        return results

    Argument.shell_complete = _patched_arg


_patch_shell_complete()




# Register modular commands
from .commands import shell_integration as shell_integration_cmd
from .commands import show as show_cmd
from .commands import open as open_cmd
from .commands import upload as upload_cmd
from .commands import diff as diff_cmd
from .commands import fetch as fetch_cmd
from .commands import pull as pull_cmd
from .commands import clone as clone_cmd
from .commands import create as create_cmd
from .commands import push as push_cmd
from .commands import sync as sync_cmd
from .commands import review as review_cmd
from .commands import ingest_attachments as ingest_attachments_cmd

shell_integration_cmd.register(cli)
show_cmd.register(cli)
open_cmd.register(cli)
upload_cmd.register(cli)
diff_cmd.register(cli)
fetch_cmd.register(cli)
pull_cmd.register(cli)
clone_cmd.register(cli)
create_cmd.register(cli)
push_cmd.register(cli)
sync_cmd.register(cli)
review_cmd.register(cli)
ingest_attachments_cmd.register(cli)


if __name__ == '__main__':
    cli()
