"""Tests for the `thrds` CLI (`thrds/cli.py`).

Use `click.testing.CliRunner` and monkeypatch `thrds.cli.SlackClient` to a
lightweight spy — the CLI's job is arg parsing, state handling, and
delegating to the library; Slack round-tripping is covered by
`test_doc_sync.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

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
    # Return a proxy that resolves to the latest-instantiated spy.
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
    """CWD → tmp_path so state.json read/write is scoped to the test."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_doc(tmp: 'PathLike', name: str = 'trainium.md', text: str | None = None) -> str:
    from pathlib import Path
    if text is None:
        text = "=== a\n\nOP a.\n"
    p = Path(tmp) / name
    p.write_text(text)
    return name


# --- init ---

def test_init_creates_state_json_with_doc_path_and_session_id(in_tmp):
    doc_name = _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['init', doc_name])
    assert result.exit_code == 0, result.output
    state = SessionState.load(in_tmp)
    assert state.doc_path == doc_name
    assert state.channel_prefix is None
    assert state.session_id  # non-empty uuid


def test_init_records_prefix_override(in_tmp):
    doc_name = _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['init', '-p', 'rw-', doc_name])
    assert result.exit_code == 0, result.output
    state = SessionState.load(in_tmp)
    assert state.channel_prefix == 'rw-'


def test_init_refuses_when_state_json_exists(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['init', 'trainium.md'])
    assert result.exit_code == 2  # click.UsageError → exit code 2
    # click prints UsageError as the last stderr line; assert exact.
    assert result.stderr.splitlines()[-1] == (
        "Error: .thrds/state.json already exists — rm to start fresh."
    )


# --- push ---

def test_push_staging_delegates_to_sync_doc_staging(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push'])
    assert result.exit_code == 0, result.output
    assert [c['mode'] for c in spy.push_calls] == ['staging']
    assert spy.push_calls[0]['dry_run'] is False
    assert spy.push_calls[0]['doc'] == Doc(threads=[DocThread(slug='a', messages=[DocMessage('OP a.')])])


def test_push_prod_delegates_to_sync_doc_prod_with_keep_staging(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push', '--prod', '--keep-staging', '-c', 'C_OARL'])
    assert result.exit_code == 0, result.output
    assert spy.push_calls == [{
        'mode': 'prod',
        'doc': Doc(threads=[DocThread(slug='a', messages=[DocMessage('OP a.')])]),
        'channel': 'C_OARL',
        'keep_staging': True,
        'dry_run': False,
    }]


def test_push_dry_run_propagates_flag(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push', '-n'])
    assert result.exit_code == 0, result.output
    assert spy.push_calls[0]['dry_run'] is True


def test_push_rejects_channel_without_prod(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push', '-c', 'C_X'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --channel requires --prod."


def test_push_rejects_keep_staging_without_prod(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push', '-k'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == "Error: --keep-staging requires --prod."


def test_push_uses_explicit_doc_path_over_state(in_tmp, spy):
    """DOC_PATH arg wins over state.doc_path."""
    _write_doc(in_tmp, name='trainium.md')
    _write_doc(in_tmp, name='other.md', text="=== b\n\nOP b.\n")
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['push', 'other.md'])
    assert result.exit_code == 0, result.output
    assert spy.push_calls[0]['doc'].threads[0].slug == 'b'


# --- pull ---

def test_pull_writes_to_disk_with_write_flag(in_tmp, spy, monkeypatch):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])

    # Script what pull returns.
    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        # Reroute the fixture's latest reference too.
        monkeypatch.setattr('thrds.cli.SlackClient', lambda *, token, channel: s)
        return s

    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    result = CliRunner().invoke(cli, ['pull', '-w'])
    assert result.exit_code == 0, result.output
    assert (in_tmp / 'trainium.md').read_text() == "=== x\n\nPulled OP.\n"


def test_pull_prints_to_stdout_without_write(in_tmp, spy, monkeypatch):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='x', messages=[DocMessage('Pulled OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)
    result = CliRunner().invoke(cli, ['pull'])
    assert result.exit_code == 0, result.output
    assert result.output == "=== x\n\nPulled OP.\n"


def test_pull_prod_passes_channel(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['pull', '--prod', '-c', 'C_OTHER'])
    assert result.exit_code == 0, result.output
    assert spy.pull_calls == [{'mode': 'prod', 'channel': 'C_OTHER'}]


# --- diff ---

def test_diff_compares_local_against_pulled(in_tmp, spy, monkeypatch):
    _write_doc(in_tmp, text="=== a\n\nLocal OP.\n")
    CliRunner().invoke(cli, ['init', 'trainium.md'])

    def factory(*, token, channel):
        s = SlackSpy(token=token, channel=channel)
        s.pull_returns = Doc(threads=[DocThread(slug='a', messages=[DocMessage('Slack OP.')])])
        return s
    monkeypatch.setattr('thrds.cli.SlackClient', factory)

    result = CliRunner().invoke(cli, ['diff'])
    assert result.exit_code == 0, result.output
    # Both docs are identical shape (one thread `a`, one message OP), so the
    # unified diff is just the OP-line change with 3 lines of context.
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

def test_archive_calls_slack_archive_and_uses_staging_channel(in_tmp, spy):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    # Simulate a prior push that set staging_channel.
    state = SessionState.load(in_tmp)
    state.staging_channel = "C_STAGE_TO_ARCHIVE"
    state.save()

    result = CliRunner().invoke(cli, ['archive'])
    assert result.exit_code == 0, result.output
    assert spy.archive_calls == ["C_STAGE_TO_ARCHIVE"]


def test_archive_is_a_no_op_when_no_staging_channel(in_tmp, spy):
    """No staging_channel in state → early return, no SlackClient constructed."""
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    result = CliRunner().invoke(cli, ['archive'])
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == ["No staging PC to archive."]
    # spy is unaccessed → SlackClient never got constructed (short-circuit).
    with pytest.raises(AttributeError, match="No SlackClient was constructed"):
        _ = spy.archive_calls


# --- error paths ---

def test_push_requires_slack_token_env(in_tmp, monkeypatch):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
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
        "Error: No thrds session state at .thrds/state.json; run `thrds init` first."
    )


def test_state_json_is_valid_json_after_init(in_tmp):
    _write_doc(in_tmp)
    CliRunner().invoke(cli, ['init', 'trainium.md'])
    data = json.loads((in_tmp / STATE_PATH).read_text())
    assert data['doc_path'] == 'trainium.md'
    assert 'session_id' in data
