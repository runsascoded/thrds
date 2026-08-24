"""Tests for shell completion."""

import subprocess

import pytest


def get_completions(words: str, cword: int = None) -> list[str]:
    """Get Click completions for the given COMP_WORDS string.

    Args:
        words: Space-separated words (e.g. "ghpr push --")
        cword: Index of word being completed (default: last word index)

    Returns:
        List of completion values
    """
    parts = words.split()
    if cword is None:
        if words.endswith(' '):
            cword = len(parts)
        else:
            cword = len(parts) - 1
    result = subprocess.run(
        ['ghpr'],
        capture_output=True,
        text=True,
        env={
            'COMP_WORDS': words,
            'COMP_CWORD': str(cword),
            '_GHPR_COMPLETE': 'bash_complete',
            'PATH': subprocess.check_output(['bash', '-c', 'echo $PATH'], text=True).strip(),
        },
    )
    completions = []
    for line in result.stdout.strip().splitlines():
        if line.startswith('plain,'):
            completions.append(line.removeprefix('plain,'))
    return completions


class TestSubcommandCompletion:
    """Test that ghpr subcommands are completed."""

    def test_all_subcommands(self):
        completions = get_completions('ghpr ')
        assert 'clone' in completions
        assert 'create' in completions
        assert 'diff' in completions
        assert 'init' in completions
        assert 'open' in completions
        assert 'pull' in completions
        assert 'push' in completions
        assert 'show' in completions
        assert 'upload' in completions
        assert 'ingest-attachments' in completions
        assert 'shell-integration' in completions

    def test_subcommand_prefix(self):
        completions = get_completions('ghpr pu')
        assert 'push' in completions
        assert 'pull' in completions
        assert 'clone' not in completions


class TestOptionCompletionOnBareTab:
    """Test that options are suggested on bare <tab> (no `-` prefix)."""

    def test_clone_bare_tab(self):
        completions = get_completions('ghpr clone ')
        assert '--no-comments' in completions
        assert '-d' in completions
        assert '--directory' in completions
        assert '--help' in completions

    def test_open_bare_tab(self):
        completions = get_completions('ghpr open ')
        assert '-g' in completions
        assert '--gist' in completions
        assert '--help' in completions

    def test_push_bare_tab(self):
        completions = get_completions('ghpr push ')
        assert '-n' in completions
        assert '--dry-run' in completions
        assert '-g' in completions
        assert '--gist' in completions
        assert '-C' in completions
        assert '--force-others' in completions

    def test_diff_bare_tab(self):
        completions = get_completions('ghpr diff ')
        assert '--no-comments' in completions
        assert '-c' in completions
        assert '--color' in completions

    def test_pull_bare_tab(self):
        completions = get_completions('ghpr pull ')
        assert '--no-comments' in completions
        assert '-n' in completions
        assert '--dry-run' in completions


class TestOptionCompletionWithPrefix:
    """Test that options filter correctly with a `-` or `--` prefix."""

    def test_push_double_dash(self):
        completions = get_completions('ghpr push --')
        assert '--dry-run' in completions
        assert '--gist' in completions
        # Short flags shouldn't appear with -- prefix
        assert '-n' not in completions

    def test_push_single_dash(self):
        completions = get_completions('ghpr push -')
        assert '-n' in completions
        assert '--dry-run' in completions

    def test_push_partial_long(self):
        completions = get_completions('ghpr push --dr')
        assert '--dry-run' in completions
        assert '--gist' not in completions


class TestUsedOptionsExcluded:
    """Test that already-used options are excluded from completions."""

    def test_push_excludes_used_flag(self):
        completions = get_completions('ghpr push -n ')
        assert '-n' not in completions
        assert '--dry-run' not in completions
        # Other flags should still appear
        assert '-g' in completions
