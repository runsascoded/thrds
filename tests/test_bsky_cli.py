"""Tests for the `thrds bsky …` subgroup (same phase-2c shape as discord)."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import SessionState
from thrds.cli import SLACK_TOKEN_ENV, cli


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.com')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.com')
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _write_doc(tmp: Path, name: str = 'draft.md', text: str = "=== a\n\nHi bsky.\n") -> str:
    (tmp / name).write_text(text)
    return name


def _init_bsky(in_tmp: Path, monkeypatch, doc_name: str = 'draft.md',
               doc_text: str = "=== a\n\nHi bsky.\n") -> Path:
    _write_doc(in_tmp, doc_name, doc_text)
    result = CliRunner().invoke(cli, ['bsky', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'thrds' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- bsky init ---


def test_bsky_init_writes_platform_bsky(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['bsky', 'init', '--no-gist', 'draft.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'thrds' / 'draft'
    state = SessionState.load(session)
    assert state.platform == 'bsky'
    assert state.doc_path == 'draft.md'


# --- bsky render + lint ---


def test_bsky_render_prints_doc_and_lints_clean(in_tmp, monkeypatch):
    _init_bsky(in_tmp, monkeypatch, doc_text="=== a\n\nShort post, no issues.\n")
    result = CliRunner().invoke(cli, ['bsky', 'render'])
    assert result.exit_code == 0
    assert result.stdout == "=== a\n\nShort post, no issues.\n"
    assert result.stderr == ""


def test_bsky_render_flags_long_paragraph(in_tmp, monkeypatch):
    """Paragraph over 300 chars → `bsky/post-too-long` warning on stderr."""
    long_para = "This is a long paragraph. " * 15  # ~390 chars
    _init_bsky(in_tmp, monkeypatch, doc_text=f"=== a\n\n{long_para}\n")
    result = CliRunner().invoke(cli, ['bsky', 'render'])
    assert result.exit_code == 0
    assert result.stdout == f"=== a\n\n{long_para}\n"
    stderr_lines = result.stderr.rstrip().split('\n')
    # Only one warning (one long paragraph). Line 3 is where the paragraph starts
    # (line 1: "=== a", line 2: "", line 3: the long paragraph).
    assert len(stderr_lines) == 1
    line = stderr_lines[0]
    assert line.startswith("draft.md:3:1: warning [bsky/post-too-long]")
    assert "300" in line
    assert "chars" in line


def test_bsky_render_flags_masked_link(in_tmp, monkeypatch):
    _init_bsky(in_tmp, monkeypatch, doc_text="=== a\n\nSee [docs](https://ex.com).\n")
    result = CliRunner().invoke(cli, ['bsky', 'render'])
    assert result.exit_code == 0
    assert result.stderr.rstrip() == (
        "draft.md:3:5: warning [bsky/masked-link] masked link '[docs](https://ex.com)' "
        "renders as literal text on Bluesky; use bare URL (auto-linkified via facets)"
    )


def test_bsky_lint_standalone_short_paragraph_reports_clean(in_tmp, monkeypatch):
    _init_bsky(in_tmp, monkeypatch, doc_text="=== a\n\nShort.\n")
    result = CliRunner().invoke(cli, ['bsky', 'lint'])
    assert result.exit_code == 0
    assert result.stderr.rstrip() == "draft.md: no issues"


def test_bsky_render_no_lint_flag_skips_the_warning_pass(in_tmp, monkeypatch):
    long_para = "x" * 400
    _init_bsky(in_tmp, monkeypatch, doc_text=f"=== a\n\n{long_para}\n")
    result = CliRunner().invoke(cli, ['bsky', 'render', '-L'])
    assert result.exit_code == 0
    assert result.stdout == f"=== a\n\n{long_para}\n"
    assert result.stderr == ""


# --- bsky open ---


def test_bsky_open_prints_gist_url_with_no_open_flag(in_tmp, monkeypatch):
    session_dir = _init_bsky(in_tmp, monkeypatch)
    state = SessionState.load(session_dir)
    state.gist_id = 'gh789'
    state.save(session_dir)

    result = CliRunner().invoke(cli, ['bsky', 'open', '-U'])
    assert result.exit_code == 0
    assert result.stderr.rstrip() == 'Opening gist gh789: https://gist.github.com/gh789'


def test_bsky_open_errors_when_no_gist(in_tmp, monkeypatch):
    _init_bsky(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['bsky', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No gist recorded — session was init'd with --no-gist."
    )


# --- platform-mismatch guard ---


def test_slack_verb_on_bsky_session_errors_clearly(in_tmp, monkeypatch):
    _init_bsky(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'bsky'; "
        "use `thrds bsky <verb>` instead of `thrds slack <verb>`."
    )


def test_bsky_verb_on_discord_session_errors_clearly(in_tmp, monkeypatch):
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['discord', 'init', '--no-gist', 'draft.md'])
    assert r1.exit_code == 0
    monkeypatch.chdir(in_tmp / 'thrds' / 'draft')

    result = CliRunner().invoke(cli, ['bsky', 'lint'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'discord'; "
        "use `thrds discord <verb>` instead of `thrds bsky <verb>`."
    )
