"""Tests for the `thrds` CLI (`thrds/cli.py`).

Use `click.testing.CliRunner` and monkeypatch `thrds.cli.SlackClient` to a
lightweight spy — the CLI's job is arg parsing, state handling, and
delegating to the library; Slack round-tripping is covered by
`test_doc_sync.py`. Gist creation is skipped via `--no-gist` on init to
avoid depending on real `gh`; a separate test covers the gist code path
via subprocess mocking.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import Doc, DocMessage, DocSyncResult, DocThread, SessionState
from thrds.cli import SLACK_TOKEN_ENV, cli
from thrds.state import STATE_PATH


@dataclass
class SlackSpy:
    """Records what CLI called; scriptable return values for pull."""
    push_calls: list[dict] = field(default_factory=list)
    pull_calls: list[dict] = field(default_factory=list)
    archive_calls: list[str] = field(default_factory=list)
    pull_returns: Doc | None = None
    sync_returns_channel: str = "C_STAGING"

    def __init__(self, *, token: str, channel: str):
        self.token = token
        self.channel = channel
        self.push_calls = []
        self.pull_calls = []
        self.archive_calls = []
        self.pull_returns = None
        self.sync_returns_channel = "C_STAGING"

    def _sync_result(self, channel: str) -> DocSyncResult:
        return DocSyncResult(
            channel=channel,
            preamble_ts="1.000000",
            thread_ts_by_slug={"a": "1.000001"},
            thread_results={},
            deleted_slugs=[],
        )

    def sync_doc_staging(self, doc, state, dry_run=False, **_kw):
        self.push_calls.append({"mode": "staging", "doc": doc, "dry_run": dry_run})
        if not dry_run:
            state.staging_channel = self.sync_returns_channel
            state.save()
        return self._sync_result(self.sync_returns_channel)

    def sync_doc_prod(self, doc, state, channel=None, keep_staging=False, dry_run=False, **_kw):
        self.push_calls.append({
            "mode": "prod",
            "doc": doc,
            "channel": channel,
            "keep_staging": keep_staging,
            "dry_run": dry_run,
        })
        resolved = channel or state.prod_channel or "C_PROD"
        if not dry_run:
            state.prod_channel = resolved
            state.save()
        return self._sync_result(resolved)

    def pull_doc_staging(self, state):
        self.pull_calls.append({"mode": "staging"})
        return self.pull_returns or Doc()

    def pull_doc_prod(self, state, channel=None):
        self.pull_calls.append({"mode": "prod", "channel": channel})
        return self.pull_returns or Doc()

    def archive_channel(self, channel: str):
        self.archive_calls.append(channel)


@pytest.fixture
def spy(monkeypatch):
    """A per-test spy the CLI's `_make_slack_client` will produce."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')

    class LatestSpy:
        def __getattr__(self, item):
            if not captured:
                raise AttributeError(
                    "No SlackClient was constructed yet — did the CLI call _make_slack_client?"
                )
            return getattr(captured[-1], item)
    return LatestSpy()


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """CWD → tmp_path so state.json read/write is scoped to the test.

    Sets git author/committer env so `git commit` in mirror.py works without
    depending on system-wide `git config`, and stamps a fake Slack token so
    CLI subcommands that call `_make_slack_client` don't error on env lookup.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Test')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'test@example.com')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Test')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'test@example.com')
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _write_doc(tmp: Path, name: str = 'trainium.md', text: str = "=== a\n\nOP a.\n") -> str:
    """Write DOC_PATH content into `tmp`; return the filename (relative)."""
    (tmp / name).write_text(text)
    return name


def _init_session(in_tmp: Path, monkeypatch, doc_name: str = 'trainium.md',
                  doc_text: str = "=== a\n\nOP a.\n") -> Path:
    """Write the doc, run `thrds init --no-gist <doc>`, chdir to session dir.

    Returns the session dir path (=``in_tmp / 'thrds' / <slug>``). Every
    test that needs a live session uses this helper; init-specific tests
    call `init` directly and inspect the target dir.
    """
    _write_doc(in_tmp, doc_name, doc_text)
    result = CliRunner().invoke(cli, ['init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'thrds' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- init ---

def test_init_creates_session_subdir_with_state_json_and_doc(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['init', '--no-gist', 'trainium.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'thrds' / 'trainium'
    assert (session / STATE_PATH).is_file()
    assert (session / 'trainium.md').read_text() == "=== a\n\nOP a.\n"
    state = SessionState.load(session)
    assert state.doc_path == 'trainium.md'
    assert state.channel_prefix is None
    assert state.session_id  # non-empty uuid
    assert state.gist_id is None  # --no-gist


def test_init_creates_git_repo_with_initial_commit(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', '--no-gist', 'trainium.md'])
    session = in_tmp / 'thrds' / 'trainium'
    assert (session / '.git').is_dir()
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init trainium']


def test_init_records_prefix_override(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', '--no-gist', '-p', 'rw-', 'trainium.md'])
    state = SessionState.load(in_tmp / 'thrds' / 'trainium')
    assert state.channel_prefix == 'rw-'


def test_init_refuses_when_target_dir_exists(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', '--no-gist', 'trainium.md'])
    result = CliRunner().invoke(cli, ['init', '--no-gist', 'trainium.md'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: Target session dir already exists: {in_tmp / 'thrds' / 'trainium'}"
    )


def test_init_creates_empty_doc_when_source_absent(in_tmp):
    """If DOC_PATH doesn't exist in CWD, init creates it empty in the session dir."""
    result = CliRunner().invoke(cli, ['init', '--no-gist', 'brandnew.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    dest = in_tmp / 'thrds' / 'brandnew' / 'brandnew.md'
    assert dest.read_text() == ""


def test_init_state_json_is_valid_pretty_printed(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', '--no-gist', 'trainium.md'])
    text = (in_tmp / 'thrds' / 'trainium' / STATE_PATH).read_text()
    data = json.loads(text)
    assert data['doc_path'] == 'trainium.md'
    assert data['staging_archived'] is False
    assert text.endswith('\n') and not text.endswith('\n\n')


def test_init_gist_flow_calls_create_gist_aligns_and_records_gist_id(in_tmp, monkeypatch):
    """Init without --no-gist: create_gist → add_remote(ssh) → align_to_remote → push.

    Mocks at the mirror module boundary (create_gist / align_to_remote / push)
    so the test doesn't need a real `gh` binary or a reachable gist URL. This
    is the right layer: `gh` and network are wrapped by mirror.py; testing the
    subprocess plumbing itself belongs in test_mirror.py.
    """
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
    monkeypatch.setattr(mirror_mod, 'push', fake_push)  # commit_and_push routes through mirror.push

    result = CliRunner().invoke(cli, ['init', 'trainium.md'])
    assert result.exit_code == 0, (result.output, result.stderr)

    session_dir = in_tmp / 'thrds' / 'trainium'
    state = SessionState.load(session_dir)
    assert state.gist_id == 'abc123'

    # Exact call sequence: create gist, align local to gist HEAD, then push the state.json commit.
    assert [c[0] for c in calls] == ['create_gist', 'align_to_remote', 'push']
    assert calls[0] == ('create_gist', str(session_dir), 'thrds: trainium', ['trainium.md'])
    assert calls[1] == ('align_to_remote', 'g', 'main')
    assert calls[2] == ('push', 'g', 'main')

    # The `g` remote is configured with the SSH URL (not HTTPS — HTTPS would prompt for auth).
    remote_url = subprocess.run(
        ['git', 'remote', 'get-url', 'g'],
        cwd=session_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_url == 'git@gist.github.com:abc123.git'


# --- push ---

def test_push_staging_delegates_to_sync_doc_staging(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert [c['mode'] for c in spy.push_calls] == ['staging']
    assert spy.push_calls[0]['dry_run'] is False
    assert spy.push_calls[0]['doc'] == Doc(threads=[DocThread(slug='a', messages=[DocMessage('OP a.')])])


def test_push_prod_delegates_to_sync_doc_prod_with_keep_staging(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push', '--prod', '--keep-staging', '-c', 'C_OARL'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.push_calls == [{
        'mode': 'prod',
        'doc': Doc(threads=[DocThread(slug='a', messages=[DocMessage('OP a.')])]),
        'channel': 'C_OARL',
        'keep_staging': True,
        'dry_run': False,
    }]


def test_push_dry_run_propagates_flag(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push', '-n'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.push_calls[0]['dry_run'] is True


def test_push_rejects_channel_without_prod(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push', '-c', 'C_X'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --channel requires --prod."


def test_push_rejects_keep_staging_without_prod(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push', '-k'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --keep-staging requires --prod."


def test_push_uses_explicit_doc_path_over_state(in_tmp, monkeypatch, spy):
    """DOC_PATH arg wins over state.doc_path."""
    session = _init_session(in_tmp, monkeypatch)
    # Add a second doc in the session dir; push at it explicitly.
    (session / 'other.md').write_text("=== b\n\nOP b.\n")
    result = CliRunner().invoke(cli, ['push', 'other.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.push_calls[0]['doc'].threads[0].slug == 'b'


def test_push_autocommits_state_and_doc(in_tmp, monkeypatch, spy):
    """A successful non-dry-run push commits state.json + doc.md to the session git."""
    session = _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: push staging', 'thrds: init trainium']


def test_push_dry_run_does_not_autocommit(in_tmp, monkeypatch, spy):
    """Dry-run push adds no commit."""
    session = _init_session(in_tmp, monkeypatch)
    CliRunner().invoke(cli, ['push', '-n'])
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init trainium']


# --- pull ---

def test_pull_writes_to_disk_with_write_flag(in_tmp, monkeypatch):
    session = _init_session(in_tmp, monkeypatch)

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    result = CliRunner().invoke(cli, ['pull', '-w'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert (session / 'trainium.md').read_text() == "=== x\n\nPulled OP.\n"


def test_pull_write_autocommits_doc(in_tmp, monkeypatch):
    """Pull --write commits the updated doc to session git."""
    session = _init_session(in_tmp, monkeypatch)

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    CliRunner().invoke(cli, ['pull', '-w'])
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: pull staging → trainium.md', 'thrds: init trainium']


def test_pull_prints_to_stdout_without_write(in_tmp, monkeypatch):
    _init_session(in_tmp, monkeypatch)

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    result = CliRunner().invoke(cli, ['pull'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.output == "=== x\n\nPulled OP.\n"


def test_pull_prod_passes_channel(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['pull', '--prod', '-c', 'C_OTHER'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.pull_calls == [{'mode': 'prod', 'channel': 'C_OTHER'}]


# --- diff ---

def test_diff_compares_local_against_pulled(in_tmp, monkeypatch):
    session = _init_session(in_tmp, monkeypatch, doc_text="=== a\n\nLocal OP.\n")

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='a', messages=[DocMessage('Slack OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    result = CliRunner().invoke(cli, ['diff'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.output.splitlines() == [
        "--- local",
        "+++ slack",
        "@@ -1,3 +1,3 @@",
        " === a",
        " ",
        "-Local OP.",
        "+Slack OP.",
    ]


# --- archive ---

def test_archive_calls_slack_archive_and_flips_flag(in_tmp, monkeypatch, spy):
    session = _init_session(in_tmp, monkeypatch)
    state = SessionState.load(session)
    state.staging_channel = "C_STAGE_TO_ARCHIVE"
    state.save(session)

    result = CliRunner().invoke(cli, ['archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.archive_calls == ["C_STAGE_TO_ARCHIVE"]
    assert SessionState.load(session).staging_archived is True


def test_archive_is_idempotent_when_already_archived(in_tmp, monkeypatch, spy):
    session = _init_session(in_tmp, monkeypatch)
    state = SessionState.load(session)
    state.staging_channel = "C_STAGE"
    state.staging_archived = True
    state.save(session)

    result = CliRunner().invoke(cli, ['archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.splitlines() == ["Already archived: C_STAGE"]
    # spy proxy would raise if SlackClient was constructed → verifies no API call.
    with pytest.raises(AttributeError, match="No SlackClient was constructed"):
        _ = spy.archive_calls


def test_archive_is_a_no_op_when_no_staging_channel(in_tmp, monkeypatch, spy):
    """No staging_channel in state → early return, no SlackClient constructed."""
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.splitlines() == ["No staging PC to archive."]
    with pytest.raises(AttributeError, match="No SlackClient was constructed"):
        _ = spy.archive_calls


# --- error paths ---

def test_push_requires_slack_token_env(in_tmp, monkeypatch):
    _init_session(in_tmp, monkeypatch)
    monkeypatch.delenv(SLACK_TOKEN_ENV, raising=False)
    result = CliRunner().invoke(cli, ['push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: {SLACK_TOKEN_ENV} not set — add a `xoxp-` user token to your env."
    )


def test_push_requires_state_json(in_tmp, spy):
    """Push before init → clear error, no Slack call."""
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No thrds session state at thrds.json; run `thrds init` first."
    )
