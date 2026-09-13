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
    responses.

    - message POSTs get sequential ids `m1`, `m2`, …
    - a thread-create POST returns `thread-1`
    - a list GET (``…/messages?limit=…``) returns ``thread_messages``
    - a single-message GET (``…/messages/{id}``, used to fetch the parent-channel
      OP during a thread reconcile) returns ``op_message``
    - PATCH / DELETE return None (edit/delete ignore the response body)
    """
    def __init__(
        self,
        thread_messages: list[dict] | None = None,
        op_message: dict | None = None,
    ):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._n = 0
        self._thread_messages = thread_messages or []
        self._op_message = op_message

    def __call__(self, method: str, path: str, data: dict | None = None):
        self.calls.append((method, path, data))
        if method == 'GET':
            # `/…/messages/{id}` (single fetch) vs `/…/messages?limit=…` (list).
            if '/messages/' in path:
                return self._op_message
            return self._thread_messages
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
    assert result.stderr.rstrip().split('\n')[-1] == "(dry run — fresh, 2 message(s), thread name 'My Thread')"
    # State untouched — no OP recorded.
    assert SessionState.load(session).discord_op_id is None


def _seed_pushed_thread(session: Path, *, thread: bool = True) -> None:
    """Record state as if a prior push landed: OP ``op1`` (thread id == OP id
    when ``thread``), thread name ``My Thread``."""
    state = SessionState.load(session)
    state.discord_op_id = 'op1'
    state.discord_thread_id = 'op1' if thread else None
    state.discord_channel_id = 'CHAN'
    state.discord_guild_id = 'GUILD'
    state.discord_thread_name = 'My Thread'
    state.save(session)


def test_discord_push_repush_edits_changed_reply_in_thread(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply CHANGED\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    rec = _CurlRecorder(
        thread_messages=[{'id': 'r1', 'content': 'reply OLD', 'type': 0}],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Reads the thread + the parent OP; OP unchanged (SKIP); reply edited in the
    # thread channel.
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
        ('PATCH', '/channels/op1/messages/r1', {'content': 'reply CHANGED'}),
    ]
    assert result.stdout == 'https://discord.com/channels/GUILD/CHAN/op1\n'
    state = SessionState.load(session)
    assert (state.discord_op_id, state.discord_thread_id) == ('op1', 'op1')


def test_discord_push_repush_edits_op_via_parent_channel(in_tmp, monkeypatch):
    # The bug the live probe found: the OP lives in the parent channel but
    # shares the thread's id, so `PATCH /channels/{thread}/messages/{op}` is a
    # 10008 — the OP edit must address the parent channel.
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP CHANGED\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    rec = _CurlRecorder(
        thread_messages=[{'id': 'r1', 'content': 'reply one', 'type': 0}],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
        ('PATCH', '/channels/CHAN/messages/op1', {'content': 'OP CHANGED'}),
    ]


def test_discord_push_repush_posts_new_reply_into_thread(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n\n+++\n\nreply two\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    rec = _CurlRecorder(
        thread_messages=[{'id': 'r1', 'content': 'reply one', 'type': 0}],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
        ('POST', '/channels/op1/messages', {'content': 'reply two'}),
    ]


def test_discord_push_repush_deletes_removed_reply_from_thread(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    # Discord returns newest-first; `list_messages` reverses to r1, r2.
    rec = _CurlRecorder(
        thread_messages=[
            {'id': 'r2', 'content': 'reply two', 'type': 0},
            {'id': 'r1', 'content': 'reply one', 'type': 0},
        ],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
        ('DELETE', '/channels/op1/messages/r2', None),
    ]


def test_discord_push_repush_noop_writes_nothing(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply one\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    rec = _CurlRecorder(
        thread_messages=[{'id': 'r1', 'content': 'reply one', 'type': 0}],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Identical content → reads only, no writes.
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
    ]


def test_discord_push_repush_dry_run_reads_but_writes_nothing(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply CHANGED\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session)
    rec = _CurlRecorder(
        thread_messages=[{'id': 'r1', 'content': 'reply one', 'type': 0}],
        op_message={'id': 'op1', 'content': 'OP body', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push', '-n'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # A re-push dry-run still reads the live thread to build the plan, but writes
    # nothing.
    assert rec.calls == [
        ('GET', '/channels/op1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/op1', None),
    ]
    assert result.stdout == ""
    assert result.stderr.rstrip().split('\n')[-1] == (
        "(dry run — re-push, 2 message(s), thread name 'My Thread')"
    )
    # State untouched by a dry run.
    assert SessionState.load(session).discord_op_id == 'op1'


def test_discord_push_repush_dry_run_requires_token(in_tmp, monkeypatch):
    # Unlike a fresh dry-run, a re-push dry-run reads the live thread, so it
    # needs a real token.
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nreply CHANGED\n")
    _set_discord_env(monkeypatch, token=None)
    _seed_pushed_thread(session)
    result = CliRunner().invoke(cli, ['discord', 'push', '-n'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Set THRDS_DISCORD_BOT_TOKEN to a Discord bot token to push.'
    )


def test_discord_push_repush_lone_op_edits_in_place(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP CHANGED\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session, thread=False)  # lone OP, no thread
    # Base-channel listing (newest-first): the OP plus an unrelated message.
    rec = _CurlRecorder(thread_messages=[
        {'id': 'other', 'content': 'unrelated', 'type': 0},
        {'id': 'op1', 'content': 'OP body', 'type': 0},
    ])
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 0, (result.output, result.stderr)
    # Reconcile scoped to the OP (no separate OP fetch — thread_id == channel);
    # the unrelated message is left untouched.
    assert rec.calls == [
        ('GET', '/channels/CHAN/messages?limit=100', None),
        ('PATCH', '/channels/CHAN/messages/op1', {'content': 'OP CHANGED'}),
    ]
    state = SessionState.load(session)
    assert (state.discord_op_id, state.discord_thread_id) == ('op1', None)


def test_discord_push_repush_lone_op_grow_refused(in_tmp, monkeypatch):
    session = _init_discord(in_tmp, monkeypatch, doc_text="OP body\n\n+++\n\nnew reply\n")
    _set_discord_env(monkeypatch)
    _seed_pushed_thread(session, thread=False)  # lone OP, no thread
    result = CliRunner().invoke(cli, ['discord', 'push'])
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        'Error: Session has a lone OP (op1) and the doc now has 2 messages; '
        'growing a lone OP into a thread on re-push is not supported. '
        'Re-init the session to push a fresh thread.'
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
    # Discord returns newest-first; `list_messages` reverses to chronological and
    # prepends the parent-channel OP (id == thread id) fetched via a single GET.
    rec = _CurlRecorder(
        thread_messages=[
            {'id': 'r2', 'content': 'second', 'type': 0},
            {'id': 'r1', 'content': 'first', 'type': 0},
        ],
        op_message={'id': 'thread-1', 'content': 'the OP', 'type': 0},
    )
    monkeypatch.setattr(DiscordClient, '_curl', lambda self, m, p, data=None: rec(m, p, data))

    result = CliRunner().invoke(cli, ['discord', 'thread'])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert rec.calls == [
        ('GET', '/channels/thread-1/messages?limit=100', None),
        ('GET', '/channels/CHAN/messages/thread-1', None),
    ]
    assert result.stdout == 'thread-1\tthe OP\nr1\tfirst\nr2\tsecond\n'
