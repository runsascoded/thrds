"""Tests for `thrds slack migrate` — the legacy → per-thread-file CLI verb."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import SessionState, ThreadEntry, ThreadTarget
from thrds.cli import SLACK_TOKEN_ENV, cli


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return tmp_path


def _legacy_session(
    tmp: Path,
    doc: str = "=== a\n\nA body.\n\n=== b\n\nB body.\n",
    **state_kw,
) -> SessionState:
    """Write a legacy single-doc session (doc + thrds.json) into ``tmp``."""
    (tmp / 'draft.md').write_text(doc)
    kw = {'doc_path': 'draft.md', 'staging_threads': {'a': '1.1', 'b': '2.2'}}
    kw.update(state_kw)
    state = SessionState.new(**kw)
    state.save(tmp)
    return state


# --- happy path ---


def test_migrate_writes_thread_files(in_tmp):
    _legacy_session(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert sorted(p.name for p in in_tmp.glob('*.md')) == ['01-a.md', '02-b.md']


def test_migrate_removes_legacy_doc(in_tmp):
    _legacy_session(in_tmp)
    CliRunner().invoke(cli, ['slack', 'migrate'])
    assert not (in_tmp / 'draft.md').exists()


def test_migrate_file_contents(in_tmp):
    _legacy_session(in_tmp, doc="=== a\n\nA body.\n\n+++\n\nA reply.\n\n=== b\n\nB body.\n")
    CliRunner().invoke(cli, ['slack', 'migrate'])
    assert (in_tmp / '01-a.md').read_text() == 'A body.\n\n+++\n\nA reply.\n'
    assert (in_tmp / '02-b.md').read_text() == 'B body.\n'


def test_migrate_updates_state(in_tmp):
    _legacy_session(in_tmp)
    CliRunner().invoke(cli, ['slack', 'migrate'])
    state = SessionState.load(in_tmp)
    assert state.threads == {
        'a': ThreadEntry(staging_ts='1.1', state='draft'),
        'b': ThreadEntry(staging_ts='2.2', state='draft'),
    }
    assert (state.doc_path, state.session_slug, state.staging_threads) == (None, 'draft', {})


def test_migrate_reports_plan_to_stderr(in_tmp):
    _legacy_session(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.stderr.rstrip().split('\n') == [
        'migrate draft.md → 2 thread file(s):',
        '  01-a.md  [draft]',
        '  02-b.md  [draft]',
        'removed draft.md; wrote 2 file(s)',
    ]


def test_migrate_reports_posted_threads_with_target(in_tmp):
    _legacy_session(in_tmp, prod_threads={'C0PROD': {'a': '9.9'}})
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.stderr.rstrip().split('\n') == [
        'migrate draft.md → 2 thread file(s):',
        '  01-a.md  [posted] → C0PROD',
        '  02-b.md  [draft]',
        'removed draft.md; wrote 2 file(s)',
    ]


def test_migrate_preamble_becomes_index_zero(in_tmp):
    _legacy_session(in_tmp, doc="Header text.\n\n=== a\n\nA body.\n")
    CliRunner().invoke(cli, ['slack', 'migrate'])
    assert sorted(p.name for p in in_tmp.glob('*.md')) == ['00-preamble.md', '01-a.md']
    assert (in_tmp / '00-preamble.md').read_text() == 'Header text.\n'


def test_migrate_frontmatter_channel_becomes_target(in_tmp):
    _legacy_session(in_tmp, doc="---\nchannel: C0FM\n---\n\n=== a\n\nA body.\n",
                    staging_threads={'a': '1.1'})
    CliRunner().invoke(cli, ['slack', 'migrate'])
    assert SessionState.load(in_tmp).threads['a'].target == ThreadTarget(channel='C0FM')


# --- dry run ---


def test_migrate_dry_run_writes_nothing(in_tmp):
    _legacy_session(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate', '-n'])
    assert result.exit_code == 0
    assert sorted(p.name for p in in_tmp.glob('*.md')) == ['draft.md']


def test_migrate_dry_run_leaves_state_untouched(in_tmp):
    _legacy_session(in_tmp)
    CliRunner().invoke(cli, ['slack', 'migrate', '-n'])
    state = SessionState.load(in_tmp)
    assert (state.doc_path, state.threads, state.staging_threads) == (
        'draft.md', {}, {'a': '1.1', 'b': '2.2'},
    )


def test_migrate_dry_run_output(in_tmp):
    _legacy_session(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate', '-n'])
    assert result.stderr.rstrip().split('\n') == [
        'migrate draft.md → 2 thread file(s):',
        '  01-a.md  [draft]',
        '  02-b.md  [draft]',
        '(dry run — nothing written)',
    ]


# --- guards ---


def test_migrate_refuses_already_migrated_session(in_tmp):
    state = SessionState.new(session_slug='s', threads={'a': ThreadEntry()})
    state.save(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: This session is already on the per-thread model (1 thread(s) in thrds.json).'
    )


def test_migrate_works_on_session_that_never_pushed(in_tmp):
    """A doc drafted but never pushed has no `staging_threads`; it's still a
    legacy-layout session and migrating it is legitimate."""
    (in_tmp / 'draft.md').write_text("=== a\n\nA body.\n")
    SessionState.new(doc_path='draft.md').save(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert sorted(p.name for p in in_tmp.glob('*.md')) == ['01-a.md']


def test_migrate_refuses_per_thread_session_with_no_threads(in_tmp):
    SessionState.new(session_slug='s').save(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Nothing to migrate: this session has no threads recorded.'
    )


def test_migrate_reports_unslugged_thread(in_tmp):
    _legacy_session(in_tmp, doc="=== a\n\nA.\n\n===\n\nUnslugged.\n")
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: Cannot migrate: thread(s) at position [1] have no `=== slug`; "
        "a slug is the thread's filename and its identity in `thrds.json` — "
        "add one to each before migrating"
    )


def test_migrate_reports_multi_channel_slug(in_tmp):
    _legacy_session(in_tmp, prod_threads={'C0A': {'a': '1.1'}, 'C0B': {'a': '2.2'}})
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: Thread 'a' was posted to multiple prod channels (C0A, C0B); "
        "the per-thread model allows one destination per thread — "
        "split it into separate threads before migrating"
    )


def test_migrate_platform_mismatch_guard(in_tmp):
    (in_tmp / 'draft.md').write_text("=== a\n\nA.\n")
    SessionState.new(doc_path='draft.md', platform='capture',
                     staging_threads={'a': '1.1'}).save(in_tmp)
    result = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'capture'; "
        "use `thrds capture <verb>` instead of `thrds slack <verb>`."
    )


# --- idempotence ---


def test_migrate_twice_refuses_second_time(in_tmp):
    _legacy_session(in_tmp)
    first = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert first.exit_code == 0
    second = CliRunner().invoke(cli, ['slack', 'migrate'])
    assert second.exit_code == 2
    assert second.stderr.splitlines()[-1] == (
        'Error: This session is already on the per-thread model (2 thread(s) in thrds.json).'
    )
