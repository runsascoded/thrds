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

import yaml
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import Doc, DocMessage, DocSyncResult, DocThread, RecoveredSession, SessionState
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
        self.scan_calls: list[dict] = []
        self.list_channels_calls: int = 0
        self.pull_returns = None
        # Either a scripted result dict, or an Exception instance to raise.
        self.scan_returns: dict[str, RecoveredSession] | Exception | None = None
        # Scripted {name: id} for `list_channels_by_name` (Exception → raise).
        self.channels_by_name: dict[str, str] | Exception | None = None
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

    def pull_doc_staging(self, state, session_dir=None):
        self.pull_calls.append({"mode": "staging", "session_dir": session_dir})
        return self.pull_returns or Doc()

    def pull_doc_prod(self, state, channel=None, session_dir=None):
        self.pull_calls.append({"mode": "prod", "channel": channel, "session_dir": session_dir})
        return self.pull_returns or Doc()

    def archive_channel(self, channel: str):
        self.archive_calls.append(channel)

    def list_channels_by_name(self) -> dict[str, str]:
        """Scriptable via `channels_by_name`; defaults to {} (raises 'not found')."""
        self.list_channels_calls += 1
        if isinstance(self.channels_by_name, Exception):
            raise self.channels_by_name
        return dict(self.channels_by_name or {})

    def scan_thrds_metadata(self, channel: str, oldest=None, latest=None,
                            cursor=None, max_pages=None, on_page=None):
        """Scriptable via `scan_returns`; defaults to {} (no sessions found).

        Records the full call (channel + scan bounds + resume) so tests can
        assert flags are forwarded correctly."""
        self.scan_calls.append({
            'channel': channel,
            'oldest': oldest,
            'latest': latest,
            'cursor': cursor,
            'max_pages': max_pages,
        })
        if isinstance(self.scan_returns, Exception):
            raise self.scan_returns
        if on_page is not None:
            # Simulate a single-page response so progress-log tests can observe it.
            on_page(1, sum(1 + len(s.thread_ts_by_slug) for s in (self.scan_returns or {}).values()))
        return self.scan_returns or {}

    def _request(self, api_method, data, **_kw):
        """Minimal `_request` shim so `cli._channel_url` can call `auth.test`."""
        if api_method == 'auth.test':
            return {'url': 'https://openathena.slack.com/'}
        raise NotImplementedError(f'SlackSpy._request({api_method!r}) not stubbed')


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
    """Write the doc, run `thrds slack init --no-gist <doc>`, chdir to session dir.

    Returns the session dir path (=``in_tmp / 'slck' / <slug>``). Every
    test that needs a live session uses this helper; init-specific tests
    call `init` directly and inspect the target dir.
    """
    _write_doc(in_tmp, doc_name, doc_text)
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', doc_name])
    assert result.exit_code == 0, (result.output, result.stderr)
    slug = Path(doc_name).stem
    session_dir = in_tmp / 'slck' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- init ---

def test_init_creates_session_subdir_with_state_json_and_doc(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'slck' / 'trainium'
    assert (session / STATE_PATH).is_file()
    assert (session / 'trainium.md').read_text() == "=== a\n\nOP a.\n"
    state = SessionState.load(session)
    assert state.doc_path == 'trainium.md'
    assert state.channel_prefix is None
    assert state.session_id  # non-empty uuid
    assert state.gist_id is None  # --no-gist


def test_init_creates_git_repo_with_initial_commit(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    session = in_tmp / 'slck' / 'trainium'
    assert (session / '.git').is_dir()
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init trainium']


def test_init_records_prefix_override(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['slack', 'init', '--no-gist', '-p', 'rw-', 'trainium.md'])
    state = SessionState.load(in_tmp / 'slck' / 'trainium')
    assert state.channel_prefix == 'rw-'


def test_init_refuses_second_run_with_no_gist(in_tmp):
    """Second `--no-gist` init on the same target: nothing to do; clean refusal."""
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: Target session dir already exists (no-gist mode): {in_tmp / 'slck' / 'trainium'}"
    )


def test_init_resumes_partial_when_gist_id_null_and_gist_flag_set(in_tmp, monkeypatch):
    """After a --no-gist init, running `thrds slack init` (without --no-gist) resumes the gist step.

    (Simulates the real recovery flow: user's earlier init failed at gist
    creation, leaving state.json with gist_id=None; a re-run picks up.)
    """
    _write_doc(in_tmp)
    # First init: no gist, leaves state.json with gist_id=None.
    r1 = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    assert r1.exit_code == 0, (r1.output, r1.stderr)
    session = in_tmp / 'slck' / 'trainium'
    assert SessionState.load(session).gist_id is None

    # Mock the mirror module boundary so the resumed gist step succeeds
    # without touching real `gh` or network.
    from thrds import mirror as mirror_mod
    calls: list[str] = []
    monkeypatch.setattr(mirror_mod, 'create_gist',
                        lambda session_dir, description, files: ('resumed_gist', 'git@gist.github.com:resumed_gist.git'))
    monkeypatch.setattr(mirror_mod, 'align_to_remote',
                        lambda session_dir, remote='g', branch='main': calls.append('align'))
    monkeypatch.setattr(mirror_mod, 'push',
                        lambda session_dir, remote='g', branch='main': calls.append('push'))

    # Second init: no --no-gist → auto-resumes the gist step.
    r2 = CliRunner().invoke(cli, ['slack', 'init', 'trainium.md'])
    assert r2.exit_code == 0, (r2.output, r2.stderr)
    assert r2.stderr.splitlines()[0].startswith('Resuming partial init at ')

    # gist_id now recorded; `g` remote configured.
    assert SessionState.load(session).gist_id == 'resumed_gist'
    remote_url = subprocess.run(
        ['git', 'remote', 'get-url', 'g'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_url == 'git@gist.github.com:resumed_gist.git'
    # align_to_remote and push both ran.
    assert calls == ['align', 'push']


def test_init_refuses_when_already_fully_initialized(in_tmp, monkeypatch):
    """Second `thrds slack init` on a session that already has gist_id → refuses cleanly."""
    _write_doc(in_tmp)
    # First init with mocked gist so it fully succeeds.
    from thrds import mirror as mirror_mod
    monkeypatch.setattr(mirror_mod, 'create_gist',
                        lambda session_dir, description, files: ('fully_done', 'git@gist.github.com:fully_done.git'))
    monkeypatch.setattr(mirror_mod, 'align_to_remote', lambda *a, **k: None)
    monkeypatch.setattr(mirror_mod, 'push', lambda *a, **k: None)
    r1 = CliRunner().invoke(cli, ['slack', 'init', 'trainium.md'])
    assert r1.exit_code == 0

    r2 = CliRunner().invoke(cli, ['slack', 'init', 'trainium.md'])
    assert r2.exit_code == 2
    session = in_tmp / 'slck' / 'trainium'
    assert r2.stderr.splitlines()[-2] == (
        f"Error: Target session dir already fully initialized (gist_id=fully_done): {session}"
    )


def test_init_refuses_when_target_dir_exists_without_state_json(in_tmp):
    """Existing target dir without a thrds.yml — refuse (not our dir)."""
    _write_doc(in_tmp)
    (in_tmp / 'slck' / 'trainium').mkdir(parents=True)
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: Target dir exists but has no thrds.yml — not a thrds session dir: {in_tmp / 'slck' / 'trainium'}"
    )


def test_init_creates_empty_doc_when_source_absent(in_tmp):
    """If DOC_PATH doesn't exist in CWD, init creates it empty in the session dir."""
    result = CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'brandnew.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    dest = in_tmp / 'slck' / 'brandnew' / 'brandnew.md'
    assert dest.read_text() == ""


def test_init_state_file_is_valid_yaml(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['slack', 'init', '--no-gist', 'trainium.md'])
    text = (in_tmp / 'slck' / 'trainium' / STATE_PATH).read_text()
    data = yaml.safe_load(text)
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

    result = CliRunner().invoke(cli, ['slack', 'init', 'trainium.md'])
    assert result.exit_code == 0, (result.output, result.stderr)

    session_dir = in_tmp / 'slck' / 'trainium'
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
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert [c['mode'] for c in spy.push_calls] == ['staging']
    assert spy.push_calls[0]['dry_run'] is False
    assert spy.push_calls[0]['doc'] == Doc(threads=[DocThread(slug='a', messages=[DocMessage('OP a.')])])


def test_push_prod_delegates_to_sync_doc_prod_with_keep_staging(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push', '--prod', '--keep-staging', '-c', 'C_OARL'])
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
    result = CliRunner().invoke(cli, ['slack', 'push', '-n'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.push_calls[0]['dry_run'] is True


def test_push_rejects_channel_without_prod(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push', '-c', 'C_X'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --channel requires --prod."


def test_push_rejects_keep_staging_without_prod(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push', '-k'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --keep-staging requires --prod."


def test_push_uses_explicit_doc_path_over_state(in_tmp, monkeypatch, spy):
    """DOC_PATH arg wins over state.doc_path."""
    session = _init_session(in_tmp, monkeypatch)
    # Add a second doc in the session dir; push at it explicitly.
    (session / 'other.md').write_text("=== b\n\nOP b.\n")
    result = CliRunner().invoke(cli, ['slack', 'push', 'other.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.push_calls[0]['doc'].threads[0].slug == 'b'


def test_push_autocommits_state_and_doc(in_tmp, monkeypatch, spy):
    """A successful non-dry-run push commits state.json + doc.md to the session git."""
    session = _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: push staging', 'thrds: init trainium']


def test_push_dry_run_does_not_autocommit(in_tmp, monkeypatch, spy):
    """Dry-run push adds no commit."""
    session = _init_session(in_tmp, monkeypatch)
    CliRunner().invoke(cli, ['slack', 'push', '-n'])
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: init trainium']


# --- pull ---

def test_pull_writes_to_disk_by_default(in_tmp, monkeypatch):
    session = _init_session(in_tmp, monkeypatch)

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    result = CliRunner().invoke(cli, ['slack', 'pull'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert (session / 'trainium.md').read_text() == "=== x\n\nPulled OP.\n"


def test_pull_autocommits_doc(in_tmp, monkeypatch):
    """Pull commits the updated doc to session git."""
    session = _init_session(in_tmp, monkeypatch)

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    CliRunner().invoke(cli, ['slack', 'pull'])
    log = subprocess.run(
        ['git', 'log', '--format=%s'],
        cwd=session, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ['thrds: pull staging → trainium.md', 'thrds: init trainium']


def test_pull_dry_run_prints_to_stdout(in_tmp, monkeypatch):
    session = _init_session(in_tmp, monkeypatch)
    before = (session / 'trainium.md').read_text()

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    result = CliRunner().invoke(cli, ['slack', 'pull', '-n'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.output == "=== x\n\nPulled OP.\n"
    assert (session / 'trainium.md').read_text() == before


def test_pull_prod_passes_channel(in_tmp, monkeypatch, spy):
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'pull', '--prod', '-c', 'C_OTHER'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # session_dir is CWD (the session dir _init_session chdir'd into) — plumbed
    # so pull_doc_* can download custom emoji into the session.
    assert spy.pull_calls == [{
        'mode': 'prod',
        'channel': 'C_OTHER',
        'session_dir': in_tmp / 'slck' / 'trainium',
    }]


# --- diff ---

def test_diff_compares_local_against_pulled(in_tmp, monkeypatch):
    session = _init_session(in_tmp, monkeypatch, doc_text="=== a\n\nLocal OP.\n")

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='a', messages=[DocMessage('Slack OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    result = CliRunner().invoke(cli, ['slack', 'diff'])
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

    result = CliRunner().invoke(cli, ['slack', 'archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert spy.archive_calls == ["C_STAGE_TO_ARCHIVE"]
    assert SessionState.load(session).staging_archived is True


def test_archive_is_idempotent_when_already_archived(in_tmp, monkeypatch, spy):
    session = _init_session(in_tmp, monkeypatch)
    state = SessionState.load(session)
    state.staging_channel = "C_STAGE"
    state.staging_archived = True
    state.save(session)

    result = CliRunner().invoke(cli, ['slack', 'archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.splitlines() == ["Already archived: C_STAGE"]
    # spy proxy would raise if SlackClient was constructed → verifies no API call.
    with pytest.raises(AttributeError, match="No SlackClient was constructed"):
        _ = spy.archive_calls


def test_archive_is_a_no_op_when_no_staging_channel(in_tmp, monkeypatch, spy):
    """No staging_channel in state → early return, no SlackClient constructed."""
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'archive'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert result.stderr.splitlines() == ["No staging PC to archive."]
    with pytest.raises(AttributeError, match="No SlackClient was constructed"):
        _ = spy.archive_calls


# --- error paths ---

def test_push_requires_slack_token_env(in_tmp, monkeypatch):
    from thrds.cli import SLACK_TOKEN_ENV_DEPRECATED
    _init_session(in_tmp, monkeypatch)
    # Delete both the canonical env var and the deprecated alias — either would
    # satisfy `_make_slack_client`, and the deprecated one may leak in from the
    # outer test environment.
    monkeypatch.delenv(SLACK_TOKEN_ENV, raising=False)
    monkeypatch.delenv(SLACK_TOKEN_ENV_DEPRECATED, raising=False)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        f"Error: {SLACK_TOKEN_ENV} not set — add a `xoxp-` (user) or `xoxb-` "
        f"(bot) Slack token to your env. Session verbs (init/push/pull/…) "
        f"need a user token; the `slack` CRUD verbs accept either."
    )


def test_deprecated_env_var_still_works_with_warning(in_tmp, monkeypatch, spy):
    """Setting only `SLACK_THRDS_USER_TOKEN` (the deprecated name) still runs
    the command; a one-time deprecation warning fires on stderr."""
    from thrds.cli import SLACK_TOKEN_ENV_DEPRECATED
    # Reset the module-level warn-once latch so the warning fires within THIS test.
    import thrds.cli as cli_mod
    monkeypatch.setattr(cli_mod, '_deprecated_env_warned', False)
    _init_session(in_tmp, monkeypatch)
    monkeypatch.delenv(SLACK_TOKEN_ENV, raising=False)
    monkeypatch.setenv(SLACK_TOKEN_ENV_DEPRECATED, 'xoxp-legacy')
    result = CliRunner().invoke(cli, ['slack', 'push', '--dry-run'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # The token gets through to the spy (proving the deprecated var was read).
    assert spy.token == 'xoxp-legacy'
    # And the deprecation notice fired on stderr.
    warning_lines = [
        line for line in result.stderr.splitlines()
        if 'deprecated' in line and SLACK_TOKEN_ENV_DEPRECATED in line
    ]
    assert len(warning_lines) == 1


def test_push_requires_state_json(in_tmp, spy):
    """Push before init → clear error, no Slack call."""
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No thrds session state at thrds.yml; run `thrds <platform> init` first."
    )


# --- open ---

def test_open_gist_default_prints_and_launches(in_tmp, monkeypatch):
    """`thrds slack open` (no flag) opens the gist URL — no Slack call needed."""
    _init_session(in_tmp, monkeypatch)
    # Simulate init that ran with a gist by stamping gist_id into state.
    state = SessionState.load()
    state.gist_id = 'abc123'
    state.save()
    opened: list[str] = []
    monkeypatch.setattr('thrds.cli.webbrowser.open', lambda url: opened.append(url))
    result = CliRunner().invoke(cli, ['slack', 'open'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert opened == ['https://gist.github.com/abc123']
    assert result.stderr.rstrip() == 'Opening gist abc123: https://gist.github.com/abc123'


def test_open_gist_no_open_flag_prints_only(in_tmp, monkeypatch):
    """`-U` prints URL, does not launch browser."""
    _init_session(in_tmp, monkeypatch)
    state = SessionState.load()
    state.gist_id = 'abc123'
    state.save()
    opened: list[str] = []
    monkeypatch.setattr('thrds.cli.webbrowser.open', lambda url: opened.append(url))
    result = CliRunner().invoke(cli, ['slack', 'open', '-U'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert opened == []


def test_open_gist_errors_when_no_gist_id(in_tmp, monkeypatch):
    """--no-gist init leaves gist_id=null; `thrds slack open` says so instead of crashing."""
    _init_session(in_tmp, monkeypatch)
    # State from _init_session already has gist_id=None (init used --no-gist).
    result = CliRunner().invoke(cli, ['slack', 'open'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No gist recorded — session was init'd with --no-gist."
    )


def test_open_staging_uses_workspace_url(in_tmp, monkeypatch, spy):
    """`-s` fetches workspace URL via auth.test and points at the staging channel."""
    _init_session(in_tmp, monkeypatch)
    state = SessionState.load()
    state.staging_channel = 'C_STAGING'
    state.save()
    opened: list[str] = []
    monkeypatch.setattr('thrds.cli.webbrowser.open', lambda url: opened.append(url))
    result = CliRunner().invoke(cli, ['slack', 'open', '-s'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert opened == ['https://openathena.slack.com/archives/C_STAGING']


def test_open_staging_errors_when_no_staging_channel(in_tmp, monkeypatch):
    """Never pushed → no staging_channel → clear error."""
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'open', '-s'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: No staging channel — run `thrds slack push` first to create one.'
    )


def test_open_prod_and_staging_mutually_exclusive(in_tmp, monkeypatch):
    """`-p` + `-s` together → clear usage error."""
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'open', '-s', '-p'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == 'Error: --prod and --staging are mutually exclusive.'


# --- recover ---

def _recovered(session_id='s-1', doc_slug='trainium', preamble_ts=None,
               threads=None, oldest_ts='1.0', newest_ts='2.0') -> RecoveredSession:
    """Compact `RecoveredSession` builder for tests."""
    return RecoveredSession(
        session_id=session_id,
        doc_slug=doc_slug,
        preamble_ts=preamble_ts,
        thread_ts_by_slug=threads or {'a': '1.001', 'b': '1.002'},
        oldest_ts=oldest_ts,
        newest_ts=newest_ts,
    )


def test_recover_writes_state_and_doc_from_scan(in_tmp, monkeypatch, spy):
    """Single-session channel → auto-select → thrds.yml + doc written; prod routing."""
    spy_pull_doc = Doc(
        preamble='hello',
        threads=[
            DocThread(slug='a', messages=[DocMessage(content='OP a.')]),
            DocThread(slug='b', messages=[DocMessage(content='OP b.')]),
        ],
    )

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {
            's-1': _recovered(
                session_id='s-1', doc_slug='trainium',
                preamble_ts='1.000',
                threads={'a': '1.001', 'b': '1.002'},
                oldest_ts='1.000', newest_ts='1.002',
            ),
        }
        s.pull_returns = spy_pull_doc
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)

    # thrds.yml shape
    state = SessionState.load()
    assert state.session_id == 's-1'
    assert state.doc_path == 'trainium.md'
    assert state.prod_channel == 'C_PROD'
    assert state.prod_preamble_ts == {'C_PROD': '1.000'}
    assert state.prod_threads == {'C_PROD': {'a': '1.001', 'b': '1.002'}}
    # No staging routing on default (--prod) recovery
    assert state.staging_channel is None
    assert state.staging_threads == {}
    # Doc content pulled + written
    assert (in_tmp / 'trainium.md').is_file()


def test_recover_staging_flag_routes_to_staging_fields(in_tmp, monkeypatch):
    """`-s` puts pointers on staging_* fields, not prod_*."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered(preamble_ts='1.000')}
        s.pull_returns = Doc()
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-s', 'C_STAGING'])
    assert result.exit_code == 0, (result.output, result.stderr)
    state = SessionState.load()
    assert state.staging_channel == 'C_STAGING'
    assert state.staging_preamble_ts == '1.000'
    assert state.staging_threads == {'a': '1.001', 'b': '1.002'}
    assert state.prod_channel is None
    assert state.prod_threads == {}


def test_recover_lists_sessions_when_multiple_and_no_id(in_tmp, monkeypatch):
    """>1 session in channel + no `-i` → prints table, exits 2, writes nothing."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {
            's-old': _recovered(session_id='s-old', doc_slug='old-doc',
                                threads={'x': '10.0'}, oldest_ts='10.0', newest_ts='10.0'),
            's-new': _recovered(session_id='s-new', doc_slug='new-doc',
                                threads={'y': '20.0'}, oldest_ts='20.0', newest_ts='20.0'),
        }
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 2
    # Table ordering: newest first (s-new before s-old)
    lines = [l for l in result.stderr.splitlines() if l.startswith('  ') and 's-' in l]
    # First data row is newest
    assert 's-new' in lines[0]
    assert 's-old' in lines[1]
    # State file not written
    assert not (in_tmp / STATE_PATH).exists()


def test_recover_with_session_id_selects_specific(in_tmp, monkeypatch):
    """`-i s-old` picks that session even when others are present."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {
            's-old': _recovered(session_id='s-old', doc_slug='old-doc',
                                threads={'x': '10.0'}),
            's-new': _recovered(session_id='s-new', doc_slug='new-doc',
                                threads={'y': '20.0'}),
        }
        s.pull_returns = Doc()
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-i', 's-old', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    state = SessionState.load()
    assert state.session_id == 's-old'
    assert state.doc_path == 'old-doc.md'
    assert state.prod_threads == {'C_PROD': {'x': '10.0'}}


def test_recover_unknown_session_id_errors(in_tmp, monkeypatch):
    """`-i` with an id that isn't in scan results → clear error, no state written."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-i', 'nope', 'C_PROD'])
    assert result.exit_code == 2
    assert "Session 'nope' not found in C_PROD" in result.stderr
    assert not (in_tmp / STATE_PATH).exists()


def test_recover_no_sessions_found_errors(in_tmp, monkeypatch):
    """Empty scan → clear error, no state written."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 2
    assert 'No thrds-metadata messages found in C_PROD' in result.stderr
    assert not (in_tmp / STATE_PATH).exists()


def test_recover_refuses_to_overwrite_existing_state(in_tmp, monkeypatch):
    """Preexisting thrds.yml in CWD → refuse (recover is for empty session dirs)."""
    _init_session(in_tmp, monkeypatch)   # leaves a thrds.yml in CWD via chdir
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 2
    assert 'refusing to overwrite' in result.stderr


def test_recover_no_write_doc_skips_pull(in_tmp, monkeypatch):
    """`-W` writes state.json but not the .md; pull is not invoked."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered(doc_slug='trainium')}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-W', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert (in_tmp / STATE_PATH).is_file()
    assert not (in_tmp / 'trainium.md').exists()


def test_recover_preserves_session_id_from_metadata(in_tmp, monkeypatch):
    """Recovered SessionState.session_id equals the metadata's session_id (not fresh UUID)."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'a-specific-uuid': _recovered(session_id='a-specific-uuid')}
        s.pull_returns = Doc()
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert SessionState.load().session_id == 'a-specific-uuid'


# --- recover: scan caps ---

def test_recover_oldest_days_forwards_unix_ts_to_scan(in_tmp, monkeypatch):
    """`-d 7` translates to `oldest=<now - 7*86400>` on the underlying scan call."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    # Freeze time so the derived `oldest` is deterministic.
    monkeypatch.setattr('thrds.cli.time.time', lambda: 1_000_000.0)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-d', '7', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[0].scan_calls == [{
        'channel': 'C_PROD',
        'oldest': 1_000_000.0 - 7 * 86400,
        'latest': None,
        'cursor': None,
        'max_pages': 50,   # default
    }]


def test_recover_default_max_pages_is_50(in_tmp, monkeypatch):
    """Default cap is 50 pages; forwarded to scan verbatim."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert captured[0].scan_calls[-1]['max_pages'] == 50


def test_recover_max_pages_zero_disables_cap(in_tmp, monkeypatch):
    """`-m 0` translates to `max_pages=None` (uncapped)."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    CliRunner().invoke(cli, ['slack', 'recover', '-m', '0', 'C_PROD'])
    assert captured[0].scan_calls[-1]['max_pages'] is None


def test_recover_translates_scan_cap_reached_to_usage_error(in_tmp, monkeypatch):
    """Scan raising `ScanCapReached` surfaces as a UsageError (exit 2) with the msg."""
    from thrds import ScanCapReached

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = ScanCapReached('hit --max-pages=50 on channel C_PROD; ...')
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 2
    assert 'hit --max-pages=50 on channel C_PROD' in result.stderr


def test_recover_progress_log_emits_per_page(in_tmp, monkeypatch):
    """`on_page` callback drives a `scan page N: M msgs (total T)` line on stderr."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered(threads={'a': '1.0', 'b': '1.1'})}
        s.pull_returns = Doc()
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # SlackSpy.scan_thrds_metadata simulates a single page whose msg count
    # is `preamble + threads = 1 + 2 = 3` for this scripted RecoveredSession.
    scan_lines = [l for l in result.stderr.splitlines() if 'scan page' in l]
    assert scan_lines == ['  scan page 1: 3 msgs (total 3)']


# --- recover: resume ---

def test_recover_latest_days_forwards_derived_unix_ts(in_tmp, monkeypatch):
    """`-D 3` translates to `latest=<now - 3*86400>` on the underlying scan call."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setattr('thrds.cli.time.time', lambda: 1_000_000.0)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '-D', '3', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[0].scan_calls[-1]['latest'] == 1_000_000.0 - 3 * 86400


def test_recover_cursor_forwards_to_scan(in_tmp, monkeypatch):
    """`--cursor TOKEN` forwards verbatim to `scan_thrds_metadata`."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    CliRunner().invoke(cli, ['slack', 'recover', '--cursor', 'CURSOR_XYZ', 'C_PROD'])
    assert captured[0].scan_calls[-1]['cursor'] == 'CURSOR_XYZ'


def test_recover_cursor_and_latest_days_mutually_exclusive(in_tmp, monkeypatch):
    """--cursor + --latest-days both specify a start point → refuse."""
    def factory(*, token, channel):
        return SlackSpy(token=token, channel=channel)

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', '--cursor', 'CX', '-D', '3', 'C_PROD'])
    assert result.exit_code == 2
    assert '--cursor and --latest-days are mutually exclusive' in result.stderr


def test_recover_scan_cap_reached_prints_next_cursor(in_tmp, monkeypatch):
    """ScanCapReached with next_cursor → stderr has `next cursor: <token>` line."""
    from thrds import ScanCapReached
    exc = ScanCapReached(
        'scan_thrds_metadata: hit --max-pages=50 on channel C_PROD; '
        'Reached back to ts=1.234.',
        next_cursor='RESUME_HERE',
        oldest_ts_reached='1.234',
        pages_scanned=50,
    )

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = exc
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'recover', 'C_PROD'])
    assert result.exit_code == 2
    # Cursor is on its own line for easy grep.
    assert '  next cursor: RESUME_HERE' in result.stderr
    # And the exception message itself is in the UsageError line.
    assert 'hit --max-pages=50' in result.stderr


# --- list-sessions ---

def test_list_sessions_prints_table(in_tmp, monkeypatch):
    """Multi-session channel → header + row per session, newest first, exit 0."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {
            's-old': _recovered(session_id='s-old', doc_slug='old-doc',
                                threads={'x': '10.0'}, newest_ts='10.0'),
            's-new': _recovered(session_id='s-new', doc_slug='new-doc',
                                threads={'y': '20.0', 'z': '20.5'}, newest_ts='20.5'),
        }
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    lines = result.stderr.splitlines()
    # Header text (there's an intro line first + column header line).
    assert any('2 sessions in C_PROD' in l for l in lines)
    assert any('session_id' in l and 'doc_slug' in l and 'threads' in l for l in lines)
    # Row order: newest_ts=20.5 (s-new) before newest_ts=10.0 (s-old).
    data_rows = [l for l in lines if 's-old' in l or 's-new' in l]
    assert 's-new' in data_rows[0]
    assert 's-old' in data_rows[1]


def test_list_sessions_no_sessions_prints_empty_message_and_exits_0(in_tmp, monkeypatch):
    """Empty channel scan → 'No thrds sessions...' on stderr, exit 0 (not 2)."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'C_PROD'])
    assert result.exit_code == 0
    assert 'No thrds-metadata sessions found in C_PROD.' in result.stderr


def test_list_sessions_forwards_scan_flags(in_tmp, monkeypatch):
    """`-d/-D/-c/-m` all reach the scan call, same shape as recover."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setattr('thrds.cli.time.time', lambda: 1_000_000.0)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, [
        'slack', 'list-sessions', '-d', '7', '-m', '100', 'C_PROD',
    ])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[-1].scan_calls == [{
        'channel': 'C_PROD',
        'oldest': 1_000_000.0 - 7 * 86400,
        'latest': None,
        'cursor': None,
        'max_pages': 100,
    }]


def test_list_sessions_writes_no_state_or_doc(in_tmp, monkeypatch):
    """`list-sessions` is read-only — no thrds.yml or .md ever written."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert not (in_tmp / STATE_PATH).exists()
    assert not (in_tmp / 'trainium.md').exists()


def test_list_sessions_singular_when_one_session(in_tmp, monkeypatch):
    """Header uses 'session' (singular) when exactly one is present."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'C_PROD'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert '1 session in C_PROD' in result.stderr
    assert '1 sessions in C_PROD' not in result.stderr


# --- channel-name resolution ---

def test_channel_id_passthrough_avoids_list_channels_api_call(in_tmp, monkeypatch):
    """Uppercase-starting refs (Slack IDs) skip the `conversations.list` lookup."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.scan_returns = {'s-1': _recovered()}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'C08XYZ001'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # No `conversations.list` calls — passthrough hit.
    assert captured[0].list_channels_calls == 0
    # And the scan was called with the ID verbatim.
    assert captured[0].scan_calls[-1]['channel'] == 'C08XYZ001'


def test_channel_name_hash_prefix_resolves_via_list_channels(in_tmp, monkeypatch):
    """`#foo` → strip `#` → look up in `conversations.list` → pass ID to scan."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001', 'bar': 'C08BAR002'}
        s.scan_returns = {'s-1': _recovered()}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', '#foo'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[0].list_channels_calls == 1
    assert captured[0].scan_calls[-1]['channel'] == 'C08FOO001'


def test_channel_bare_name_resolves_via_list_channels(in_tmp, monkeypatch):
    """Bare `foo` (no `#`) also resolves; the leading `#` is optional."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        s.scan_returns = {'s-1': _recovered()}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'foo'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[0].scan_calls[-1]['channel'] == 'C08FOO001'


def test_channel_hash_uppercase_still_resolves_via_lookup(in_tmp, monkeypatch):
    """`#FOO` → hash-prefix bypasses the uppercase-passthrough rule → case-insensitive lookup."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        s.scan_returns = {'s-1': _recovered()}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', '#FOO'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert captured[0].list_channels_calls == 1
    assert captured[0].scan_calls[-1]['channel'] == 'C08FOO001'


def test_channel_uppercase_no_hash_passes_through_as_id(in_tmp, monkeypatch):
    """`FOO` (no `#`, uppercase-first) → treated as a Slack ID via the passthrough rule.

    Trade-off: `foo` and `FOO` don't behave identically. Documented — Slack
    channel names are lowercase, and uppercase-starting refs are almost
    always IDs. Prepend `#` when in doubt.
    """
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        s.scan_returns = {'s-1': _recovered()}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', 'FOO'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Passthrough hit, no lookup.
    assert captured[0].list_channels_calls == 0
    assert captured[0].scan_calls[-1]['channel'] == 'FOO'


def test_channel_name_not_found_shows_available_hint(in_tmp, monkeypatch):
    """Unknown name → UsageError listing (some of) the available names."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {f'ch-{i}': f'C{i:04d}' for i in range(20)}
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', '#does-not-exist'])
    assert result.exit_code == 2
    assert "Channel '#does-not-exist' not found" in result.stderr
    # Preview shows first 5 sorted names.
    assert "'ch-0'" in result.stderr


def test_channel_name_missing_scope_error_is_helpful(in_tmp, monkeypatch):
    """RuntimeError with `missing_scope` → UsageError telling user to add scope or use ID."""
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = RuntimeError('Slack API error: missing_scope')
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    result = CliRunner().invoke(cli, ['slack', 'list-sessions', '#foo'])
    assert result.exit_code == 2
    assert 'channels:read' in result.stderr
    assert 'groups:read' in result.stderr


def test_push_prod_channel_flag_resolves_name(in_tmp, monkeypatch):
    """`push --prod --channel #foo` resolves to `C08FOO001` before sync_doc_prod."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'push', '--prod', '--channel', '#foo'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # The captured `sync_doc_prod` call should carry the RESOLVED ID.
    prod_calls = [c for c in captured[-1].push_calls if c['mode'] == 'prod']
    assert prod_calls[0]['channel'] == 'C08FOO001'


def test_pull_prod_channel_flag_resolves_name(in_tmp, monkeypatch):
    """`pull --prod --channel #foo` resolves to `C08FOO001` before pull_doc_prod."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'pull', '--prod', '--channel', '#foo'])
    assert result.exit_code == 0, (result.output, result.stderr)
    prod_pulls = [c for c in captured[-1].pull_calls if c['mode'] == 'prod']
    assert prod_pulls[0]['channel'] == 'C08FOO001'


def test_diff_prod_channel_flag_resolves_name(in_tmp, monkeypatch):
    """`diff --prod --channel #foo` resolves before pull_doc_prod (used by diff)."""
    captured: list[SlackSpy] = []

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.channels_by_name = {'foo': 'C08FOO001'}
        s.pull_returns = Doc()
        captured.append(s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    _init_session(in_tmp, monkeypatch)
    result = CliRunner().invoke(cli, ['slack', 'diff', '--prod', '--channel', '#foo'])
    assert result.exit_code == 0, (result.output, result.stderr)
    prod_pulls = [c for c in captured[-1].pull_calls if c['mode'] == 'prod']
    assert prod_pulls[0]['channel'] == 'C08FOO001'
