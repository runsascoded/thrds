"""Tests for the `thrds capture …` subgroup.

Capture-only sessions have no platform target — just a git-tracked doc dir
optionally mirrored to a gist. Covers:

- `capture init` writes `platform='capture'` into `thrds.json`
- `capture init` with gist enabled: mocked `create_gist` / `align` / `push`
  boundary, gist_id ends up in state (same shape as `slack init`)
- `capture push` autocommits the doc + state
- `capture open` prints / opens the gist URL (or errors if no gist)
- Platform-mismatch guard: `capture <verb>` on a slack-inited session (and
  vice versa) errors with a clear message, no half-execution
- Init cross-platform reject: `capture init` on a slack-inited partial-init
  session dir refuses instead of overwriting
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import SessionState
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.state import STATE_PATH


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """CWD → tmp_path so state.json read/write is scoped to the test.

    Sets git author/committer env so `git commit` in mirror.py works without
    depending on system-wide `git config`. Stamps a slack-token env var too
    (some helpers construct a client eagerly; capture verbs don't need it,
    but leaving it unset would flake other paths).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.com')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.com')
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _write_doc(tmp: Path, name: str = 'notes.md', text: str = "=== a\n\nHello.\n") -> str:
    (tmp / name).write_text(text)
    return name


def _init_capture(in_tmp: Path, monkeypatch, doc_name: str = 'notes.md') -> Path:
    """Write the doc, run `thrds capture init --no-gist <doc>`, chdir to session dir."""
    _write_doc(in_tmp, doc_name)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'thrds' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


def _init_slack(in_tmp: Path, monkeypatch, doc_name: str = 'notes.md') -> Path:
    """Same, but slack-inited — for the mismatch tests."""
    _write_doc(in_tmp, doc_name)
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'thrds' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- capture init ---


def test_capture_init_no_gist_writes_platform_capture(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'thrds' / 'notes'
    state = SessionState.load(session)
    assert state.platform == 'capture'
    assert state.doc_path == 'notes.md'
    assert state.gist_id is None
    assert state.channel_prefix is None
    assert state.session_id  # non-empty uuid


def test_capture_init_no_gist_creates_git_repo_with_initial_commit(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    session = in_tmp / 'thrds' / 'notes'
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init notes']


def test_capture_init_reports_generic_hint_no_staging_pc(in_tmp):
    """`slack init` prints the staging PC name; `capture init` does not
    (no channel concept). Both print `Initialized session ...` + cd hint."""
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    lines = result.stderr.rstrip().splitlines()
    # Exact shape: "Initialized session <uuid> at <dir>", then "cd <path> to work on this doc".
    # No staging PC line, no gist line (--no-gist).
    assert len(lines) == 2
    assert lines[0].startswith("Initialized session ")
    assert " at " in lines[0]
    assert lines[1].startswith("cd ") and lines[1].endswith(" to work on this doc")


def test_capture_init_gist_flow_calls_create_gist_and_records_gist_id(in_tmp, monkeypatch):
    """Init without --no-gist under `capture`: same mirror.create_gist / align /
    push sequence as `slack init` (shared `_do_init`). Gist_id lands in state."""
    _write_doc(in_tmp)
    calls: list[tuple] = []

    from thrds import mirror as mirror_mod

    def fake_create_gist(session_dir, description, files):
        calls.append(('create_gist', str(session_dir), description, list(files)))
        return ('abc123', 'git@gist.github.com:abc123.git')

    def fake_align(session_dir, remote='g', branch='main'):
        calls.append(('align_to_remote', remote, branch))

    def fake_push(session_dir, remote='g', branch='main'):
        calls.append(('push', remote, branch))

    monkeypatch.setattr(mirror_mod, 'create_gist', fake_create_gist)
    monkeypatch.setattr(mirror_mod, 'align_to_remote', fake_align)
    monkeypatch.setattr(mirror_mod, 'push', fake_push)

    result = CliRunner().invoke(cli, ['capture', 'init', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)

    session_dir = in_tmp / 'thrds' / 'notes'
    state = SessionState.load(session_dir)
    assert state.platform == 'capture'
    assert state.gist_id == 'abc123'

    assert [c[0] for c in calls] == ['create_gist', 'align_to_remote', 'push']
    assert calls[0] == ('create_gist', str(session_dir), 'thrds: notes', ['notes.md'])


def test_capture_init_with_gist_reports_gist_url_hint(in_tmp, monkeypatch):
    _write_doc(in_tmp)
    from thrds import mirror as mirror_mod
    monkeypatch.setattr(mirror_mod, 'create_gist',
                        lambda *a, **k: ('abc123', 'git@gist.github.com:abc123.git'))
    monkeypatch.setattr(mirror_mod, 'align_to_remote', lambda *a, **k: None)
    monkeypatch.setattr(mirror_mod, 'push', lambda *a, **k: None)

    result = CliRunner().invoke(cli, ['capture', 'init', 'notes.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    lines = result.stderr.rstrip().splitlines()
    # Exact shape: init line, gist hint, cd hint.
    assert len(lines) == 3
    assert lines[0].startswith("Initialized session ")
    assert lines[1] == "Gist: https://gist.github.com/abc123"
    assert lines[2].startswith("cd ") and lines[2].endswith(" to work on this doc")


# --- capture push ---


def test_capture_push_autocommits_doc_and_state(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    # Modify the doc to give autocommit something new to snapshot.
    (session_dir / 'notes.md').write_text("=== a\n\nUpdated.\n")
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    # init commit + capture-push commit (newest-first).
    assert log == ['thrds: capture push', 'thrds: init notes']


def test_capture_push_prints_no_gist_hint_when_missing(in_tmp, monkeypatch):
    """--no-gist session → push commits locally + prints an explicit no-gist note."""
    session_dir = _init_capture(in_tmp, monkeypatch)
    (session_dir / 'notes.md').write_text("=== a\n\nUpdated.\n")
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 0
    assert result.stderr.rstrip().splitlines() == ['(no gist configured — commit only)']


# --- capture open ---


def test_capture_open_prints_gist_url_with_no_open_flag(in_tmp, monkeypatch):
    session_dir = _init_capture(in_tmp, monkeypatch)
    # Fake a gist_id so `open` has something to point at (no-gist init leaves it None).
    state = SessionState.load(session_dir)
    state.gist_id = 'def456'
    state.save(session_dir)

    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.rstrip().splitlines() == [
        'Opening gist def456: https://gist.github.com/def456',
    ]


def test_capture_open_errors_when_no_gist(in_tmp, monkeypatch):
    _init_capture(in_tmp, monkeypatch)  # no-gist init → gist_id stays None
    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No gist recorded — session was init'd with --no-gist."
    )


# --- platform-mismatch guard ---


def test_slack_push_on_capture_session_errors_clearly(in_tmp, monkeypatch):
    """`slack push` inside a capture-inited session dir → clear platform-mismatch error."""
    _init_capture(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'capture'; "
        "use `thrds capture <verb>` instead of `thrds slack <verb>`."
    )


def test_capture_push_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    """Symmetric: `capture push` inside a slack-inited session → clear error."""
    _init_slack(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds capture <verb>`."
    )


def test_capture_open_on_slack_session_errors_clearly(in_tmp, monkeypatch):
    """Symmetric for `open` too."""
    _init_slack(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['capture', 'open', '-U'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds capture <verb>`."
    )


# --- cross-platform init reject ---


def test_capture_init_rejects_slack_partial_init_dir(in_tmp, monkeypatch):
    """Partial `slack init` on disk (gist_id=None) → `capture init <same doc>` refuses
    rather than silently rewriting the session's platform. Message tells the user to
    re-run with the original platform (or delete the dir to reset)."""
    # Bootstrap a partial slack init: no-gist mode leaves state.gist_id=None even
    # after "successful" init, so it looks the same as a real partial-init dir.
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'notes.md'])
    assert r1.exit_code == 0
    # But we need to also NOT be in the "already exists (no-gist mode)" branch —
    # so make the state look partial by clearing gist_id AND removing the --no-gist
    # early-exit hint (which requires re-invoking capture init WITHOUT --no-gist).
    r2 = CliRunner().invoke(cli, ['capture', 'init', 'notes.md'])
    assert r2.exit_code == 2
    # The first `if existing.platform != platform:` check fires before any of
    # the gist-id branches, so the message names the mismatch cleanly.
    assert r2.stderr.splitlines()[-1] == (
        "Error: Target session dir was inited for platform 'slack'; "
        "use `thrds slack init` (or delete the dir to reset)."
    )


def test_slack_init_rejects_capture_partial_init_dir(in_tmp, monkeypatch):
    """Symmetric: `slack init` on a capture-inited dir refuses."""
    _write_doc(in_tmp)
    r1 = CliRunner().invoke(cli, ['capture', 'init', '--no-gist', 'notes.md'])
    assert r1.exit_code == 0
    r2 = CliRunner().invoke(cli, ['slack', 'init', 'notes.md'])
    assert r2.exit_code == 2
    assert r2.stderr.splitlines()[-1] == (
        "Error: Target session dir was inited for platform 'capture'; "
        "use `thrds capture init` (or delete the dir to reset)."
    )
