"""Tests for `slck diff` on a per-thread session.

`diff` answers "what would `pull` change?" *before* the write, which `pull -n`
doesn't — that dumps the Slack side without comparing. Before this it was
legacy-single-doc only: handed a thread file it fell into the DOC_PATH path,
compared against an empty doc, and reported every thread as deleted.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget, tracking
from thrds.cli import SLACK_TOKEN_ENV, cli


@dataclass
class PullSpy:
    """SlackClient stand-in returning canned Slack-side threads."""
    returns: dict[str, DocThread] = field(default_factory=dict)
    prod_returns: dict[str, DocThread] = field(default_factory=dict)
    slug_calls: list[list[str] | None] = field(default_factory=list)

    def __init__(self, *, token, channel):
        self.returns = {}
        self.prod_returns = {}
        self.slug_calls = []

    def list_channels_by_name(self) -> dict[str, str]:
        return {}

    @staticmethod
    def _filter(canned, slugs):
        return list(canned.values()) if slugs is None else [
            t for s, t in canned.items() if s in set(slugs)
        ]

    def pull_threads_staging(self, state, session_dir=None, slugs=None, download_emoji=True):
        self.slug_calls.append(None if slugs is None else list(slugs))
        return self._filter(self.returns, slugs)

    def pull_promoted_threads(self, state, session_dir=None, slugs=None, download_emoji=True):
        return self._filter(self.prod_returns, slugs)


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
        'gamma: never pushed to Slack — nothing to compare against.'
    ]


def _promote(session_dir, slug: str) -> None:
    state = SessionState.load(session_dir)
    entry = state.thread(slug)
    entry.state = 'posted'
    entry.target = ThreadTarget(channel='C0PROD', thread_ts='0.1')
    entry.posted_msg_ts = ['7.7']
    state.save(session_dir)


def test_promoted_thread_diffs_against_prod_not_staging(session, spy):
    """`pull` overrides a posted thread with prod, so diff must compare there —
    otherwise a hand-edit made at the target reads as a change pull would undo."""
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Frozen staging copy.'), 'beta': thread('beta', 'Beta OP.')}
    spy.prod_returns = {'alpha': thread('alpha', 'Alpha OP.')}
    assert _run().stdout == ''


def test_promoted_thread_reports_a_prod_side_edit(session, spy):
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    spy.prod_returns = {'alpha': thread('alpha', 'Alpha OP, hand-edited in prod.')}
    assert _run().stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +1 @@',
        '-Alpha OP.',
        '+Alpha OP, hand-edited in prod.',
    ]


def test_promoted_thread_falls_back_to_staging_when_prod_returns_nothing(session, spy):
    """Mirrors `pull`: an unfetchable prod copy leaves the staging copy standing."""
    _promote(session, 'alpha')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    spy.prod_returns = {}
    assert _run().stdout == ''


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


# --- three-way classification (with a fetched base) ---


def _set_base(session_dir, files: dict[str, str]) -> None:
    """Init a git repo and plant `upstream` at ``files``, as `fetch` would."""
    for args in (['init', '-q', '-b', 'main'],
                 ['config', 'user.email', 't@example.com'],
                 ['config', 'user.name', 'T']):
        subprocess.run(['git', *args], cwd=session_dir, check=True, capture_output=True)
    ref = tracking.ref_name(tracking.COMPOSITE)
    tree = tracking.build_tree(session_dir, files)
    tracking.snapshot(session_dir, ref, 'upstream', tree, 'thrds: fetch upstream')


BOTH_BASES = {'01-alpha.md': 'Alpha OP.\n', '02-beta.md': 'Beta OP.\n'}


def test_remote_only_change_classifies_as_pulls(session, spy):
    """base == local, Slack moved: the hunk is Slack's, `pull` applies it."""
    _set_base(session, BOTH_BASES)
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, edited in Slack.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run()
    assert result.stderr.splitlines() == [
        '01-alpha.md: changed in Slack — `pull` applies it',
    ]
    assert result.stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +1 @@',
        '-Alpha OP.',
        '+Alpha OP, edited in Slack.',
    ]


def test_local_only_change_classifies_as_pushs_and_flips_direction(session, spy):
    """base == remote, we moved: the hunk is ours, and the diff reads
    slack → local — what `push` sends, not what `pull` would undo."""
    _set_base(session, BOTH_BASES)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert result.stderr.splitlines() == [
        '01-alpha.md: changed locally — `push` sends it',
    ]
    assert result.stdout.splitlines() == [
        '--- 01-alpha.md (slack)',
        '+++ 01-alpha.md (local)',
        '@@ -1 +1 @@',
        '-Alpha OP.',
        '+Alpha OP, edited locally.',
    ]


def test_both_sides_changed_is_a_conflict(session, spy):
    _set_base(session, BOTH_BASES)
    (session / '01-alpha.md').write_text('Alpha OP, edited locally.\n')
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, edited in Slack.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run()
    assert result.stderr.splitlines() == [
        '01-alpha.md: CONFLICT — both sides changed since the last fetch',
    ]
    assert result.stdout.splitlines() == [
        '--- 01-alpha.md (local)',
        '+++ 01-alpha.md (slack)',
        '@@ -1 +1 @@',
        '-Alpha OP, edited locally.',
        '+Alpha OP, edited in Slack.',
    ]


def test_both_sides_converged_is_silent(session, spy):
    """local == remote is nothing-to-do regardless of what the base says."""
    _set_base(session, BOTH_BASES)
    (session / '01-alpha.md').write_text('Same everywhere now.\n')
    spy.returns = {
        'alpha': thread('alpha', 'Same everywhere now.'),
        'beta': thread('beta', 'Beta OP.'),
    }
    result = _run()
    assert (result.stdout, result.stderr) == ('', '')


def test_thread_missing_from_the_base_still_classifies(session, spy):
    """A thread pushed since the last fetch has an empty base: local matches
    nothing recorded, Slack matches local — silent; if they differ, both
    moved relative to '' and CONFLICT is the honest answer."""
    _set_base(session, {'01-alpha.md': 'Alpha OP.\n'})
    (session / '02-beta.md').write_text('Beta OP, v2.\n')
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert result.stderr.splitlines() == [
        '02-beta.md: CONFLICT — both sides changed since the last fetch',
    ]


def test_no_base_prints_the_plain_diff_with_a_hint(session, spy):
    """Sessions that never ran `fetch` keep the two-way behavior; the hint
    names the verb that upgrades it, once, on stderr."""
    spy.returns = {
        'alpha': thread('alpha', 'Alpha OP, edited in Slack.'),
        'beta': thread('beta', 'Beta OP, edited in Slack.'),
    }
    result = _run()
    assert result.stderr.splitlines() == [
        '(run `slck fetch` first to classify changes as local vs. Slack-side)',
    ]
    assert [l for l in result.stdout.splitlines() if l.startswith('---')] == [
        '--- 01-alpha.md (local)',
        '--- 02-beta.md (local)',
    ]


def test_no_base_and_no_changes_stays_silent(session, spy):
    """The hint earns its line only when there's a diff to classify."""
    spy.returns = {'alpha': thread('alpha', 'Alpha OP.'), 'beta': thread('beta', 'Beta OP.')}
    result = _run()
    assert (result.stdout, result.stderr) == ('', '')
