"""Tests for the `thrds discord …` subgroup.

Phase 2c scope (see specs/done/discord-platform.md): init + render + lint +
open, no live push. Prod delivery on Discord is copy-paste (self-bots violate
ToS), so the subgroup captures iteration history and surfaces MD-compat
warnings — it doesn't post.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import SessionState
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.state import STATE_PATH


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """CWD → tmp_path so state.json read/write is scoped to the test.

    Git author/committer env for mirror.commit; a fake slack token stamped in
    only because some code paths that fetch a client (unused by discord verbs
    today) would otherwise error on env lookup.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.com')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.com')
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _write_doc(tmp: Path, name: str = 'draft.md', text: str = "=== a\n\nHello.\n") -> str:
    (tmp / name).write_text(text)
    return name


def _init_discord(in_tmp: Path, monkeypatch, doc_name: str = 'draft.md',
                  doc_text: str = "=== a\n\nHello.\n") -> Path:
    """Write the doc, run `thrds discord init --no-gist <doc>`, chdir to session dir."""
    _write_doc(in_tmp, doc_name, doc_text)
    result = CliRunner().invoke(cli, ['discord', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'thrds' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- discord init ---


def test_discord_init_writes_platform_discord(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['discord', 'init', '--no-gist', 'draft.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'thrds' / 'draft'
    state = SessionState.load(session)
    assert state.platform == 'discord'
    assert state.doc_path == 'draft.md'
    assert state.gist_id is None


# --- discord render ---


def test_discord_render_prints_doc_verbatim(in_tmp, monkeypatch):
    _init_discord(in_tmp, monkeypatch, doc_text="=== a\n\nJust prose. No issues.\n")
    result = CliRunner().invoke(cli, ['discord', 'render'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stdout == "=== a\n\nJust prose. No issues.\n"
    # No lint issues → nothing on stderr from the render command.
    assert result.stderr == ""


def test_discord_render_autoruns_lint_and_prints_warnings_to_stderr(in_tmp, monkeypatch):
    text = "=== a\n\nSee [the docs](https://ex.com) and ping @alice.\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'render'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Stdout is still the raw MD (unchanged by lint).
    assert result.stdout == text
    # Stderr has both warnings, in file order.
    stderr_lines = result.stderr.rstrip().split('\n')
    assert stderr_lines == [
        "draft.md:3:5: warning [discord/masked-link] masked link '[the docs](https://ex.com)' "
        "renders as literal text in normal Discord messages; use bare URL",
        "draft.md:3:41: warning [discord/raw-mention] raw @alice won't ping in Discord; "
        "use <@user_id> to mention",
    ]


def test_discord_render_no_lint_flag_skips_the_warning_pass(in_tmp, monkeypatch):
    text = "=== a\n\nSee [x](https://ex.com).\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'render', '-L'])
    assert result.exit_code == 0
    assert result.stdout == text
    assert result.stderr == ""


# --- discord lint ---


def test_discord_lint_reports_issues_to_stderr(in_tmp, monkeypatch):
    text = "=== a\n\nHere's [a link](https://ex.com).\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'lint'])
    assert result.exit_code == 0  # warnings, not errors
    assert result.stderr.rstrip() == (
        "draft.md:3:8: warning [discord/masked-link] masked link '[a link](https://ex.com)' "
        "renders as literal text in normal Discord messages; use bare URL"
    )
    # `lint` produces no stdout — findings go to stderr.
    assert result.stdout == ""


def test_discord_lint_clean_doc_reports_no_issues(in_tmp, monkeypatch):
    _init_discord(in_tmp, monkeypatch, doc_text="=== a\n\nAll good.\n")
    result = CliRunner().invoke(cli, ['discord', 'lint'])
    assert result.exit_code == 0
    assert result.stderr.rstrip() == "draft.md: no issues"


# --- discord open ---


def test_discord_open_prints_gist_url_with_no_open_flag(in_tmp, monkeypatch):
    session_dir = _init_discord(in_tmp, monkeypatch)
    # Fake a gist_id so open has something to point at.
    state = SessionState.load(session_dir)
    state.gist_id = 'gh123'
    state.save(session_dir)

    result = CliRunner().invoke(cli, ['discord', 'open', '-U'])
    assert result.exit_code == 0
    assert result.stderr.rstrip() == 'Opening gist gh123: https://gist.github.com/gh123'


def test_discord_open_errors_when_no_gist(in_tmp, monkeypatch):
    _init_discord(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['discord', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No gist recorded — session was init'd with --no-gist."
    )


# --- platform-mismatch guard (symmetric to test_capture_cli.py) ---


def test_slack_verb_on_discord_session_errors_clearly(in_tmp, monkeypatch):
    _init_discord(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'discord'; "
        "use `thrds discord <verb>` instead of `thrds slack <verb>`."
    )


def test_discord_verb_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    """Symmetric: slack-inited session + `discord lint` → clear mismatch error."""
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'draft.md'])
    assert r1.exit_code == 0
    slug = Path('draft.md').stem
    monkeypatch.chdir(in_tmp / 'thrds' / slug)

    result = CliRunner().invoke(cli, ['discord', 'lint'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds discord <verb>`."
    )
