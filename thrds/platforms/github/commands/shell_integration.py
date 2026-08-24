"""Shell integration command."""

from os import environ
from pathlib import Path
from click import Choice
from utz import err
from utz.cli import arg


CLICK_COMPLETE_VAR = '_GHPR_COMPLETE'
SHELL_COMPLETE_ENVS = {
    'bash': 'bash_source',
    'zsh': 'zsh_source',
    'fish': 'fish_source',
}


def get_click_completion(shell: str) -> str:
    """Generate Click's shell completion script for the given shell."""
    import io
    import sys
    from click.shell_completion import get_completion_class
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        return ''
    from ..cli import cli
    comp = comp_cls(cli, {}, 'ghpr', CLICK_COMPLETE_VAR)
    # Suppress Click's Bash version warning (system bash may be old, but user's shell is fine)
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        return comp.source()
    finally:
        sys.stderr = old_stderr


def shell_integration(shell: str | None) -> None:
    """Output shell aliases and completion for ghpr commands.

    Usage:
        # Bash/Zsh: Add to your ~/.bashrc or ~/.zshrc:
        eval "$(ghpr shell-integration bash)"

        # Fish: Add to your ~/.config/fish/config.fish:
        ghpr shell-integration fish | source

        # Or save to a file and source it:
        ghpr shell-integration bash > ~/.ghpr-aliases.sh
        echo 'source ~/.ghpr-aliases.sh' >> ~/.bashrc
    """
    # Auto-detect shell if not specified
    if not shell:
        shell_env = environ.get('SHELL', '')
        if 'fish' in shell_env:
            shell = 'fish'
        elif 'zsh' in shell_env:
            shell = 'zsh'
        else:
            shell = 'bash'  # default

    # Get the shell files from the ghpr package
    pkg_dir = Path(__file__).parent.parent
    shell_file = pkg_dir / 'shell' / f'ghpr.{shell if shell != "zsh" else "bash"}'

    if not shell_file.exists():
        err(f"Error: Shell integration file not found: {shell_file}")
        exit(1)

    # Output Click completion script (subcommands, flags, options)
    completion = get_click_completion(shell)
    if completion:
        # Filter out Click's Bash version warning (it's not a shell comment)
        lines = [
            line for line in completion.splitlines()
            if not line.startswith('Shell completion is not supported')
        ]
        print('\n'.join(lines))
        print()

    # Output aliases and functions
    with open(shell_file, 'r') as f:
        print(f.read())


def register(cli):
    """Register command with CLI."""

    @cli.command(name='shell-integration')
    @arg('shell', type=Choice(['bash', 'zsh', 'fish']), required=False)
    def shell_integration_cmd(shell):
        """Output shell aliases and functions for ghpr."""
        shell_integration(shell)
