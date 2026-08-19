"""Tests for `slck diff` on a per-thread session.

`diff` answers "what would `pull` change?" *before* the write, which `pull -n`
doesn't — that dumps the Slack side without comparing. Before this it was
legacy-single-doc only: handed a thread file it fell into the DOC_PATH path,
compared against an empty doc, and reported every thread as deleted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry
from thrds.cli import SLACK_TOKEN_ENV, cli


@dataclass
class PullSpy:
    """SlackClient stand-in returning canned Slack-side threads."""
    returns: dict[str, DocThread] = field(default_factory=dict)
    slug_calls: list[list[str] | None] = field(default_factory=list)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.slug_calls = []

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    def pull_threads_staging(self, state, session_dir=None, slugs=None):
        self.slug_calls.append(None if slugs is None else list(slugs))
        wanted = self.returns if slugs is None else {
            s: t for s, t in self.returns.items() if s in set(slugs)
        }
        return list(wanted.values())


def thread(slug: str, *contents: str) -> DocThread:
    return DocThread(
        slug=slug,
        messages=[
            DocMessage(content=c, author=None if i == 0 else 'someone')
            for i, c in enumerate(contents)
        ],
    )


@pytest.fixture
def spy(monkeypatch):
    the_spy = PullSpy(token='xoxp-fake', channel='')
    monkeypatch.setattr('thrds.cli.SlackClient', lambda *, token, channel: the_spy)
    monkeypatch.setenv(SLACK_TOKEN_ENV, 'xoxp-fake')
    return the_spy


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n')
    (tmp_path / '02-beta.md').write_text('Beta OP.\n')
    SessionState.new(
        session_slug='s',
        staging_channel='C0STAGE',
        threads={'alpha': ThreadEntry(staging_ts='1.1'), 'beta': ThreadEntry(staging_ts='2.2')},
    ).save(tmp_path)
    return tmp_path


def _run(*args):
    return CliRunner().invoke(cli, ['slack', 'diff', *args], catch_exceptions=False)


def test_unchanged_threads_print_nothing(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert result.exit_code == 0
    assert result.stdout == ''


def test_changed_thread_diffs_local_against_slack(session, spy):
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, edited in Slack.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run()
    assert result.stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +1 @@',
        '-Alpha OP.',
        '+Alpha OP, edited in Slack.',
    ]


def test_a_side_is_local_b_side_is_slack(session, spy):
    """Direction is "what `pull` would do": Slack content arrives as `+`."""
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP.', 'A reply added in Slack.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    assert _run().stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +1,5 @@',
        ' Alpha OP.',
        '+',
        '++++ @someone',
        '+',
        '+A reply added in Slack.',
    ]


def test_diffs_every_changed_thread_in_file_order(session, spy):
    spy.returns = {
        'beta': thread('beta', 'Beta edited.'),
        'alpha': thread('alpha', 'Alpha edited.'),
    }
    headers = [l for l in _run().stdout.splitlines() if l.startswith('---')]
    assert headers == ['--- 01-alpha.md (local)', '--- 02-beta.md (local)']


def test_single_thread_arg_restricts_and_limits_the_fetch(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha edited.'), 'beta': thread('beta', 'Beta edited.')}
    result = _run('alpha')
    assert [l for l in result.stdout.splitlines() if l.startswith('---')] == [
        '--- 01-alpha.md (local)',
    ]
    assert spy.slug_calls == [['alpha']]


def test_filename_arg_resolves_to_the_same_thread(session, spy):
    spy.returns = {'alpha': thread('alpha', 'Alpha edited.')}
    assert _run('01-alpha.md').stdout == _run('alpha').stdout
    assert spy.slug_calls == [['alpha'], ['alpha']]


def test_unknown_thread_arg_errors_with_available_slugs(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'diff', 'nope'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: No thread 'nope' in this session; available: alpha, beta"
    )


def test_unpushed_thread_arg_reports_rather_than_diffing(session, spy, tmp_path):
    (tmp_path / '03-gamma.md').write_text('Gamma OP.\n')
    result = _run('gamma')
    assert result.exit_code == 0
    assert result.stdout == ''
    assert result.stderr.splitlines() == [
        'gamma: never pushed to staging — nothing on the Slack side to compare.'
    ]


def test_unpushed_threads_are_skipped_in_the_all_threads_case(session, spy, tmp_path):
    """`pull` wouldn't touch a never-pushed file, so `diff` has nothing to say."""
    (tmp_path / '03-gamma.md').write_text('Gamma OP.\n')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert result.stdout == ''
    assert spy.slug_calls == [['alpha', 'beta']]


def test_deleted_slack_op_shows_the_file_going_away(session, spy):
    """A thread whose OP was deleted in Slack: `pull` would empty the file."""
    spy.returns = {'alpha': thread('alpha'), 'beta': thread('beta', 'Beta OP.')}
    assert _run().stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +0,0 @@',
        '-Alpha OP.',
    ]


def test_locally_deleted_file_shows_slack_side_as_added(session, spy, tmp_path):
    (tmp_path / '01-alpha.md').unlink()
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    assert _run().stdout.splitlines() == [
        '--- alpha.md (local)',
        '+++ alpha.md (slack)',
        '@@ -0,0 +1 @@',
        '+Alpha OP.',
    ]


def test_noncanonical_local_formatting_is_reported_not_hidden(session, spy, tmp_path):
    """`pull` overwrites the file, so formatting it would rewrite is a change."""
    (tmp_path / '01-alpha.md').write_text('\n\nAlpha OP.\n\n\n')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    assert _run().stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1,5 +1 @@',
        '-',
        '-',
        ' Alpha OP.',
        '-',
        '-',
    ]


def test_prod_rejected_on_per_thread_session(session, spy):
    result = CliRunner().invoke(cli, ['slack', 'diff', '-p'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Per-thread sessions diff against staging only; a promoted '
        'thread is tracked by its `posted_ts`.'
    )
    assert spy.slug_calls == []


def test_session_with_no_staged_threads_says_so(tmp_path, monkeypatch, spy):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '01-alpha.md').write_text('Alpha OP.\n')
    SessionState.new(session_slug='s', staging_channel='C0S').save(tmp_path)
    result = _run()
    assert result.exit_code == 0
    assert result.stdout == ''
    assert result.stderr.splitlines() == ['No staged threads to diff.']
