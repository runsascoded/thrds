"""Tests for the `thrds.json` → `thrds.yml` / per-remote-pointers migration.

The extension change is the version boundary: old code can't half-read new
state (no file by its name), and new code reads the legacy JSON explicitly —
through `ThreadEntry`'s pre-pointers kwargs — then replaces it on the next
save. The commit that follows must record the swap, or the gist mirror keeps
serving stale state under the old name.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from thrds import SessionState, ThreadEntry, ThreadTarget
from thrds.state import LEGACY_STATE_PATH, STATE_PATH, RemotePointer


def _write_legacy(root: Path) -> None:
    """A thrds.json as the pre-pointers schema wrote it (nulls and all)."""
    (root / LEGACY_STATE_PATH).write_text(json.dumps({
        'session_id': 'fixed-uuid',
        'session_slug': 's',
        'staging_channel': 'C0STAGE',
        'threads': {
            'alpha': {
                'staging_ts': '1.1',
                'target': None,
                'state': 'draft',
                'posted_ts': None,
                'posted_url': None,
                'posted_msg_ts': None,
            },
            'beta': {
                'staging_ts': '2.2',
                'target': {'channel': 'C0PROD', 'thread_ts': '0.1'},
                'state': 'posted',
                'posted_ts': '9.9',
                'posted_url': 'https://x.slack.com/p9',
                'posted_msg_ts': ['9.9', '9.10'],
            },
        },
    }, indent=2) + '\n')


def test_legacy_json_loads_into_pointer_entries(tmp_path):
    _write_legacy(tmp_path)
    state = SessionState.load(tmp_path)
    assert state.threads['alpha'].remotes == {'staging': RemotePointer(ts='1.1')}
    assert state.threads['beta'].remotes == {
        'staging': RemotePointer(ts='2.2'),
        'prod': RemotePointer(
            ts='9.9', msg_ts=['9.9', '9.10'], channel='C0PROD',
            thread_ts='0.1', url='https://x.slack.com/p9',
        ),
    }


def test_legacy_accessors_read_the_pointer_map(tmp_path):
    _write_legacy(tmp_path)
    beta = SessionState.load(tmp_path).threads['beta']
    assert (
        beta.staging_ts, beta.target, beta.posted_ts, beta.posted_url, beta.posted_msg_ts,
    ) == (
        '2.2', ThreadTarget(channel='C0PROD', thread_ts='0.1'),
        '9.9', 'https://x.slack.com/p9', ['9.9', '9.10'],
    )


def test_save_replaces_json_with_yml(tmp_path):
    _write_legacy(tmp_path)
    SessionState.load(tmp_path).save(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [str(STATE_PATH)]
    reloaded = SessionState.load(tmp_path)
    assert reloaded.threads['beta'].posted_msg_ts == ['9.9', '9.10']


def test_yaml_round_trip_preserves_entries(tmp_path):
    state = SessionState.new(
        session_slug='s',
        staging_channel='C0STAGE',
        threads={'alpha': ThreadEntry(staging_ts='1.1', target=ThreadTarget(channel='C0P'))},
    )
    state.save(tmp_path)
    assert SessionState.load(tmp_path).threads == state.threads


def test_ts_values_survive_yaml_as_strings(tmp_path):
    """`safe_dump` quotes number-like strings, so a full-precision Slack ts
    round-trips as the exact string it was."""
    ts = '1755718799.123456'
    SessionState.new(
        session_slug='s', threads={'a': ThreadEntry(staging_ts=ts)},
    ).save(tmp_path)
    loaded = SessionState.load(tmp_path)
    assert loaded.threads['a'].staging_ts == ts


def test_unquoted_ts_is_rejected_with_a_quoting_hint(tmp_path):
    """A hand-edit that drops the quotes turns a ts into a float; precision
    loss is silent, so refusing to load is the only honest reading."""
    (tmp_path / STATE_PATH).write_text(
        'session_id: fixed-uuid\n'
        'threads:\n'
        '  a:\n'
        '    state: draft\n'
        '    remotes:\n'
        '      staging: {ts: 1755718799.123456}\n'
    )
    with pytest.raises(ValueError) as e:
        SessionState.load(tmp_path)
    assert str(e.value) == (
        'ts parsed as a number (1755718799.123456) — Slack ts values must be '
        'quoted strings in thrds.yml (unquoted YAML turns them into floats '
        'and can lose precision)'
    )


def test_upstream_is_derived_from_lifecycle_state():
    assert ThreadEntry(staging_ts='1.1').upstream == 'staging'
    assert ThreadEntry(state='posted', posted_ts='9.9').upstream == 'prod'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_next_commit_records_the_file_swap(tmp_path):
    """After `save` migrates, `_stage_paths` adds the tracked-but-deleted
    thrds.json so the commit (and thus the gist) records the rename."""
    from thrds.cli import _stage_paths
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 'T')
    _write_legacy(tmp_path)
    _git(tmp_path, 'add', str(LEGACY_STATE_PATH))
    _git(tmp_path, 'commit', '-q', '-m', 'legacy')

    SessionState.load(tmp_path).save(tmp_path)
    paths = _stage_paths(tmp_path, [str(STATE_PATH)])
    assert paths == [str(STATE_PATH), str(LEGACY_STATE_PATH)]
    _git(tmp_path, 'add', *paths)
    _git(tmp_path, 'commit', '-q', '-m', 'migrate')
    assert _git(tmp_path, 'ls-files') == str(STATE_PATH)


def test_stage_paths_is_a_nop_before_migration_and_for_fresh_sessions(tmp_path):
    from thrds.cli import _stage_paths
    _git(tmp_path, 'init', '-q', '-b', 'main')
    # Fresh session: no thrds.json was ever tracked.
    assert _stage_paths(tmp_path, [str(STATE_PATH)]) == [str(STATE_PATH)]
    # Unmigrated session: thrds.json still on disk (no save happened yet).
    _write_legacy(tmp_path)
    assert _stage_paths(tmp_path, [str(STATE_PATH)]) == [str(STATE_PATH)]
