"""Tests for the `thrds discord …` subgroup.

Two delivery models (see specs/done/discord-platform.md, specs/discord-push.md):
paste (init + render + lint + open — post *as you*, since self-bots violate
ToS) and bot push (push + thread — post *as a bot*: OP in the channel, replies
in a thread). Push tests stub `DiscordClient._curl` so `sync` runs for real
with no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from thrds import SessionState
from thrds.cli import (
    DISCORD_BOT_TOKEN_ENV,
    DISCORD_CHANNEL_ENV,
    DISCORD_GUILD_ENV,
    SLACK_TOKEN_ENV,
    cli,
)
from thrds.discord import DiscordClient
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
    session_dir = in_tmp / 'dscrd' / slug
    monkeypatch.chdir(session_dir)
    return session_dir


# --- discord init ---


def test_discord_init_writes_platform_discord(in_tmp):
    _write_doc(in_tmp)
    result = CliRunner().invoke(cli, ['discord', 'init', '--no-gist', 'draft.md'])
    assert result.exit_code == 0, (result.output, result.stderr)
    session = in_tmp / 'dscrd' / 'draft'
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
    # Masked links render fine on Discord since 2023; the two active rules are
    # tables and raw @mentions.
    text = "=== a\n\nPing @alice and see:\n|---|\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'render'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Stdout is still the raw MD (unchanged by lint).
    assert result.stdout == text
    # Stderr has both warnings, in file order.
    stderr_lines = result.stderr.rstrip().split('\n')
    assert stderr_lines == [
        "draft.md:3:6: warning [discord/raw-mention] raw @alice won't ping in Discord; "
        "use <@user_id> to mention",
        "draft.md:4:1: warning [discord/table] markdown table doesn't render in Discord; "
        "use a code block or bullets",
    ]


def test_discord_render_no_lint_flag_skips_the_warning_pass(in_tmp, monkeypatch):
    text = "=== a\n\nPing @alice about it.\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'render', '-L'])
    assert result.exit_code == 0
    assert result.stdout == text
    assert result.stderr == ""


# --- discord lint ---


def test_discord_lint_reports_issues_to_stderr(in_tmp, monkeypatch):
    text = "=== a\n\nHey @bob, thanks!\n"
    _init_discord(in_tmp, monkeypatch, doc_text=text)
    result = CliRunner().invoke(cli, ['discord', 'lint'])
    assert result.exit_code == 0  # warnings, not errors
    assert result.stderr.rstrip() == (
        "draft.md:3:5: warning [discord/raw-mention] raw @bob won't ping in Discord; "
        "use <@user_id> to mention"
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
    monkeypatch.chdir(in_tmp / 'slck' / slug)

    result = CliRunner().invoke(cli, ['discord', 'lint'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "Error: This session was inited for platform 'slack'; "
        "use `thrds slack <verb>` instead of `thrds discord <verb>`."
    )


# --- discord push / thread (bot delivery) ---


class _CurlRecorder:
    """Stub for `DiscordClient._curl`: records every call and returns canned
    responses. Message POSTs get sequential ids `m1`, `m2`, …; a thread-create
    returns `thread-1`; GET returns a scripted message list."""
    def __init__(self, get_response: list[dict] | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._n = 0
        self._get_response = get_response or []

    def __call__(self, method: str, path: str, data: dict | None = None):
        self.calls.append((method, path, data))
        if method == 'GET':
            return self._get_response
        if method == 'POST' and path.endswith('/threads'):
            return {'id': 'thread-1'}
        if method == 'POST' and path.endswith('/messages'):
            self._n += 1
            return {'id': f'm{self._n}'}
        return None


def _set_discord_env(monkeypatch, token='bot-tok', channel='CHAN', guild='GUILD'):
    if token is not None:
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV, token)
    else:
        monkeypatch.delenv(DISCORD_BOT_TOKEN_ENV, raising=False)
    if channel is not None:
        monkeypatch.setenv(DISCORD_CHANNEL_ENV, channel)
    if guild is not None:
        monkeypatch.setenv(DISCORD_GUILD_ENV, guild)


def test_discord_push_fresh_creates_op_thread_and_replies(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch)
    rec = _CurlRecorder()
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push', '-N', 'My Thread'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # OP → channel; thread opened off it; reply → the new thread channel.
    assert rec.calls == [
        ('POST', '/channels/CHAN/messages', {'content': 'OP body'}),
        ('POST', '/channels/CHAN/messages/m1/threads', {'name': 'My Thread'}),
        ('POST', '/channels/thread-1/messages', {'content': 'reply one'}),
    ]
    # Permalink to the OP on stdout.
    assert result.stdout == 'https://discord.com/channels/GUILD/CHAN/m1\n'
    # State records where the push landed, for a future reconcile.
    state = SessionState.load(session)
    assert (
        state.discord_op_id,
        state.discord_thread_id,
        state.discord_channel_id,
        state.discord_guild_id,
        state.discord_thread_name,
    ) == ('m1', 'thread-1', 'CHAN', 'GUILD', 'My Thread')


def test_discord_push_lone_op_opens_no_thread(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="just the OP, no replies\n")
    _set_discord_env(monkeypatch)
    rec = _CurlRecorder()
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [
        ('POST', '/channels/CHAN/messages', {'content': 'just the OP, no replies'}),
    ]
    state = SessionState.load(session)
    assert (state.discord_op_id, state.discord_thread_id) == ('m1', None)


def test_discord_push_dry_run_posts_nothing_and_needs_no_token(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch, token=None)  # no credentials — dry run makes no calls
    rec = _CurlRecorder()
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push', '-n', '-N', 'My Thread'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == []
    # Plan previewed on stderr; nothing on stdout.
    assert result.stdout == ""
    assert result.stderr.rstrip().split('\n')[-1] == "(dry run — 2 message(s), thread name 'My Thread')"
    # State untouched — no OP recorded.
    assert SessionState.load(session).discord_op_id is None


def test_discord_push_refuses_repush(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch)
    rec = _CurlRecorder()
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    first = CliRunner().invoke(cli, ['discord', 'push', '-N', 'My Thread'])
    assert first.exit_code == 0, (first.output, first.stderr)
    second = CliRunner().invoke(cli, ['discord', 'push', '-N', 'My Thread'])
    assert second.exit_code == 2
    assert second.stderr.splitlines()[-1] == (
        'Error: Session already pushed (OP m1); re-push/edit is not yet '
        'implemented — see specs/discord-push.md.'
    )


def test_discord_push_requires_token(in_tmp, monkeypatch):
    _init_discord(in_tmp, monkeypatch, doc_text="OP body\n")
    _set_discord_env(monkeypatch, token=None)
    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Set THRDS_DISCORD_BOT_TOKEN to a Discord bot token to push.'
    )


def test_discord_thread_dumps_messages(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n")
    _set_discord_env(monkeypatch)
    state = SessionState.load(session)
    state.discord_thread_id = 'thread-1'
    state.save(session)
    # Discord returns newest-first; `list_messages` reverses to chronological.
    rec = _CurlRecorder(get_response=[
        {'id': 'm2', 'content': 'second', 'type': 0},
        {'id': 'm1', 'content': 'first', 'type': 0},
    ])
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'thread'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [('GET', '/channels/thread-1/messages?limit=100', None)]
    assert result.stdout == 'm1\tfirst\nm2\tsecond\n'
