"""Command-line interface for `thrds` sessions.

Two platform subgroups today:

- ``thrds slack …`` — the primary workflow: draft locally in `.md`, sync to
  a Slack staging PC, tweak in-Slack, pull back. Session verbs
  (`init`/`push`/`pull`/`diff`/`archive`/`open`/`list-sessions`/`recover`)
  plus low-level CRUD verbs
  (`history`/`thread`/`rm`/`post`/`edit`/`permalink`).
- ``thrds capture …`` — capture-only sessions: git + gist trajectory, no
  platform target. For drafting posts you'll paste into a channel manually
  (Discord, elsewhere) while still capturing iteration history to a gist.

Each session lives in its own directory (ghpr-style:
``<git-root-or-cwd>/thrds/<slug>/``) with its own private git repo (nested
`.git/` is invisible to any surrounding project's git). A secret gist created
at init becomes the ``g`` remote; state-mutating verbs auto-commit + push, so
the mirror always reflects the current session state.

Platform is stamped into ``thrds.json`` at init and every subsequent verb
guards on it: running `thrds slack push` inside a capture-inited session
errors with a clear message rather than blowing up deep inside Slack code.

Slack access is via **either** token type, set in ``THRDS_SLACK_TOKEN``
(``SLACK_THRDS_USER_TOKEN`` is a deprecated alias, still honored with a
one-time warning). The token type determines which verbs are usable:

- **User token** (``xoxp-…``) — required for the **session verbs**
  (``slack init`` / ``push`` / ``pull`` / ``diff`` / ``archive`` /
  ``list-sessions`` / ``recover`` / ``open``). The session workflow's
  point is "draft locally in `.md`, sync to a staging PC, tweak the
  posts in Slack (as you), pull back, push again"; because those
  in-Slack tweaks are your Slack user's own posts, only a token owned
  by you can ``chat.update`` them. See
  `multi-thread-posts-and-capture.md`.
- **Bot token** (``xoxb-…``) — sufficient for the ``slack`` CRUD verbs
  (``history`` / ``thread`` / ``rm`` / ``post`` / ``edit`` /
  ``permalink``) whenever the bot is only editing / deleting its own
  posts (Slack lets ``chat.update`` / ``chat.delete`` touch a bot's
  own messages fine). Also sufficient for programmatic
  ``SlackClient.sync()`` / ``sync_linked()`` when the bot owns the
  content lifecycle end-to-end (the watchy shape: bot renders mrkdwn,
  bot posts, bot reconciles later).

Required scopes (add under **OAuth & Permissions** — under **User Token
Scopes** for a user token, **Bot Token Scopes** for a bot token):

- ``chat:write`` — post + edit + delete messages (all sync + CRUD verbs)
- ``groups:write`` — create + archive staging private channels (``slack init``,
  ``push``, ``archive``); session-verb-only
- ``groups:read`` — read private channel history + resolve names to IDs
- ``channels:read`` — resolve public channel names to IDs (only if you
  push/pull to public channels)
- ``users:read`` — resolve foreign-author names on ``pull``;
  session-verb-only
- ``emoji:read`` — download custom workspace emoji on ``pull``;
  session-verb-only
- ``chat:write.customize`` — per-message sender overrides (``Msg`` with
  ``username`` / ``icon_url`` / ``icon_emoji``, or the CRUD ``post``
  verb's ``-u`` / ``-i`` / ``-e`` flags). Add if you use them.
- ``reactions:read`` — `SenderChangePolicy` pre-flight (`sync` aborts a
  sender-change repost if a target has reactions and
  ``lose_reactions_ok=False``); library-level, not used by any CLI verb.

Metadata visibility is app-scoped (Slack only returns your app's
metadata to your app); no extra scope required for ``slack recover``.
"""
from __future__ import annotations

import os
import time
import webbrowser
from pathlib import Path

import click

from . import mirror
from .doc import Doc
from .chrome import split as split_chrome
from .md import diff_docs, diff_texts, parse_doc, serialize_doc, serialize_thread
from .migrate import apply_migration, plan_migration
from .replay import ReplayError, plan_replay, verify_plan, write_replay
from .slack import ScanCapReached, SlackClient
from .state import STATE_PATH, SessionState, ThreadTarget
from .threadfile import (
    find_thread,
    parse_thread_filename,
    read_threads,
    thread_filename,
    thread_files,
)


def err(*msg):
    """stderr `print`. A plain function (not `partial(print, file=sys.stderr)`)
    so `sys.stderr` is resolved at call time — click's CliRunner patches it
    per-invocation, and a partial baked-in reference would bypass that."""
    click.echo(" ".join(str(m) for m in msg), err=True)


SLACK_TOKEN_ENV = 'THRDS_SLACK_TOKEN'
SLACK_TOKEN_ENV_DEPRECATED = 'SLACK_THRDS_USER_TOKEN'
# Optional bot token, used only to DM you when a thread is promoted. It has to
# be a *different* identity from the one that posts: a message you authored
# with your own user token is attributed to you and advances your own read
# cursor, so it never produces an unread or a push notification. A bot DM does.
SLACK_BOT_TOKEN_ENV = 'THRDS_SLACK_BOT_TOKEN'

_deprecated_env_warned = False


def _make_slack_client() -> SlackClient:
    """Instantiate a `SlackClient` from ``THRDS_SLACK_TOKEN`` (or the
    deprecated ``SLACK_THRDS_USER_TOKEN`` alias, with a one-time warning).

    Either a user token (``xoxp-…``) or a bot token (``xoxb-…``) works;
    the session verbs require a user token because they edit human-typed
    posts in Slack, while the ``slack`` CRUD verbs and programmatic
    ``SlackClient.sync()`` are fine with either token type (bot fine as
    long as the bot owns the content it's editing / deleting). See the
    module docstring for the full breakdown.

    ``channel`` is initialized to ``""`` — every doc-level method
    (``sync_doc_*``, ``pull_doc_*``) swaps ``self.channel`` internally per
    operation, so the init value doesn't matter.
    """
    global _deprecated_env_warned
    token = os.environ.get(SLACK_TOKEN_ENV)
    if not token:
        token = os.environ.get(SLACK_TOKEN_ENV_DEPRECATED)
        if token and not _deprecated_env_warned:
            err(
                f"warning: {SLACK_TOKEN_ENV_DEPRECATED} is deprecated; "
                f"rename to {SLACK_TOKEN_ENV}. Both are honored for now."
            )
            _deprecated_env_warned = True
    if not token:
        raise click.UsageError(
            f"{SLACK_TOKEN_ENV} not set — add a `xoxp-` (user) or `xoxb-` "
            f"(bot) Slack token to your env. Session verbs (init/push/pull/…) "
            f"need a user token; the `slack` CRUD verbs accept either."
        )
    return SlackClient(token=token, channel="")


def _notify_promoted(client: SlackClient, slug: str, channel: str, message_ts: str) -> None:
    """DM the user that ``slug`` just went out, via the bot token if configured.

    Best-effort by construction: a failure here must never turn a successful
    post into a command that looks failed. Warn and move on.

    Why a bot token rather than the user token already in hand: `promote` posts
    *as you*, and Slack doesn't notify you about your own messages — it marks
    them read as it posts them. A notification therefore requires a second
    identity, and a bot DM is the supported, non-deprecated way to get one.
    """
    bot_token = os.environ.get(SLACK_BOT_TOKEN_ENV)
    if not bot_token:
        err(
            f"  (no {SLACK_BOT_TOKEN_ENV} — no DM sent; posts made with your own "
            f"token don't notify you)"
        )
        return
    try:
        user_id = client.bot_ids[0]
        link = client.permalink(message_ts)
        bot = SlackClient(token=bot_token, channel=user_id)
        bot.post(f"Posted *{slug}* to <#{channel}>: {link}", raw=True)
        err(f"  DM sent to {user_id}")
    except Exception as e:  # noqa: BLE001 — notification must not fail the promote
        err(f"  warning: promote succeeded but DM failed: {e}")


def _load_state(expected_platform: str | None = None) -> SessionState:
    """Load ``thrds.json`` from CWD or exit clearly.

    ``expected_platform`` (set by every platform-group verb to its own name)
    catches mis-platforming — running ``thrds slack push`` inside a session
    that was inited via ``thrds capture init`` errors immediately with a
    clear message, instead of blowing up deep inside slack-specific code.
    """
    try:
        state = SessionState.load()
    except FileNotFoundError as e:
        raise click.UsageError(str(e))
    if expected_platform is not None and state.platform != expected_platform:
        raise click.UsageError(
            f"This session was inited for platform {state.platform!r}; "
            f"use `thrds {state.platform} <verb>` instead of "
            f"`thrds {expected_platform} <verb>`."
        )
    return state


def _resolve_doc_path(state: SessionState, arg: str | None) -> str:
    """Resolve the doc path: explicit arg wins, else state.doc_path, else error."""
    path = arg if arg is not None else state.doc_path
    if path is None:
        raise click.UsageError(
            "No doc path — pass DOC_PATH or run `thrds <platform> init <doc.md>` first."
        )
    return path


def _load_doc(state: SessionState, arg: str | None) -> Doc:
    """Parse the doc file identified by ``arg`` (or ``state.doc_path``)."""
    path = _resolve_doc_path(state, arg)
    return parse_doc(Path(path).read_text()).doc


def _absorb_file_chrome(
    session_dir: Path,
    threads: list,
    state: SessionState,
    client,
    dry_run: bool,
) -> dict:
    """Take a chrome line written into a thread *file* as a destination, not text.

    Hand-seeding a draft — writing the body in an editor with `→ #chan` at the
    top — is the same gesture as writing one in Slack, and should mean the same
    thing. Left alone it would post as the first line of the message, which is
    both wrong and hard to notice.

    Applies the target to ``state``, drops the line from the thread's OP, and
    rewrites the file without it: chrome's home is the staged message, which a
    push is about to render for real.
    """
    applied: dict[str, ThreadTarget] = {}
    for tf, thread in zip(thread_files(session_dir), threads, strict=True):
        ours = [m for m in thread.messages if m.author is None]
        if not ours:
            continue
        body, chrome = split_chrome(ours[0].content)
        if chrome is None:
            continue
        ours[0].content = body
        resolved = client._resolve_chrome_channel(chrome)
        if resolved is not None:
            target = ThreadTarget(channel=resolved, thread_ts=chrome.thread_ts)
            state.thread(tf.slug).target = target
            applied[tf.slug] = target
        if not dry_run:
            tf.path.write_text(serialize_thread(thread))
    if applied and not dry_run:
        # Persisted here rather than left to the sync: the target is settled
        # the moment it's read, and a sync that fails shouldn't lose it.
        state.save()
    return applied


def _print_sync_summary(kind: str, result) -> None:
    """Human-readable summary of a `DocSyncResult` to stderr."""
    err(f"{kind}: channel={result.channel}")
    if result.preamble_ts:
        err(f"  preamble: {result.preamble_ts}")
    for slug, ts in result.thread_ts_by_slug.items():
        err(f"  {slug}: {ts}")
    if result.deleted_slugs:
        err(f"  deleted (terraform): {result.deleted_slugs}")


def _autocommit(session_root: Path, paths: list[str], message: str) -> None:
    """Commit ``paths`` (relative to session_root) and push to gist if configured.

    No-op if ``session_root`` isn't a git repo (rare — only if user manually
    removed `.git/`). No-op if there's no `g` remote (``thrds <p> init --no-gist``).
    """
    if not mirror.is_git_repo(session_root):
        return
    try:
        mirror.commit_and_push(session_root, paths, message)
    except mirror.MirrorError as e:
        err(f"warning: mirror commit/push failed: {e}")


def _do_gist_init(target: Path, state: SessionState, slug: str) -> str:
    """Create gist, add remote, align local HEAD, commit + push state.json.

    Shared between the fresh-init path and the resume path — either can leave
    the session in the same "gist populated + state.json committed" end state.
    Returns the new gist_id.
    """
    try:
        gist_id, git_url = mirror.create_gist(
            target,
            description=f'thrds: {slug}',
            files=[f'{slug}.md'],
        )
    except mirror.MirrorError as e:
        raise click.UsageError(
            f"gist creation failed:\n{e}\n\n"
            "Re-run `thrds <platform> init` with `--no-gist` to skip the gist mirror."
        )
    mirror.add_remote(target, state.gist_remote, git_url)
    mirror.align_to_remote(target, remote=state.gist_remote)
    state.gist_id = gist_id
    state.save(target)
    mirror.commit_and_push(
        target,
        [str(STATE_PATH)],
        f'thrds: init {slug} (gist {gist_id})',
        remote=state.gist_remote,
    )
    return gist_id


def _do_init(
    doc_path: str,
    no_gist: bool,
    channel_prefix: str | None,
    platform: str,
) -> tuple[Path, SessionState, bool]:
    """Shared init flow for every platform's `init` verb.

    Handles slug derivation, target-dir resolution, auto-resume of a partial
    init (matching platform required), fresh-init doc copy/create, `git init`,
    state save, and gist creation (unless ``no_gist``).

    Returns ``(target, state, was_resume)`` — callers use ``was_resume`` to
    decide which completion hints to print. The resume path itself prints
    only ``Gist created: …`` to stderr and does no other output.
    """
    doc_p = Path(doc_path)
    slug = doc_p.stem
    if not slug:
        raise click.UsageError(f"cannot derive a slug from DOC_PATH: {doc_path!r}")
    target = mirror.resolve_session_dir(Path.cwd(), slug)

    if target.exists():
        state_path = target / STATE_PATH
        if not state_path.is_file():
            raise click.UsageError(
                f"Target dir exists but has no {STATE_PATH} — not a thrds session dir: {target}"
            )
        try:
            existing = SessionState.load(target)
        except Exception as e:
            raise click.UsageError(f"Target session dir has an invalid {STATE_PATH}: {e}")
        if existing.platform != platform:
            raise click.UsageError(
                f"Target session dir was inited for platform {existing.platform!r}; "
                f"use `thrds {existing.platform} init` (or delete the dir to reset)."
            )
        if existing.gist_id is not None:
            raise click.UsageError(
                f"Target session dir already fully initialized (gist_id={existing.gist_id}): {target}\n"
                f"Use `thrds {platform} open` to browse, or delete the dir and re-run to reset."
            )
        if no_gist:
            # gist_id=None + --no-gist = re-running the same intent; nothing to do.
            raise click.UsageError(
                f"Target session dir already exists (no-gist mode): {target}"
            )
        err(f"Resuming partial init at {target} (gist step)...")
        gist_id = _do_gist_init(target, existing, slug)
        err(f"Gist created: https://gist.github.com/{gist_id}")
        return target, existing, True

    target.mkdir(parents=True)

    dest_md = target / f'{slug}.md'
    if doc_p.exists() and doc_p.is_file():
        dest_md.write_text(doc_p.read_text())
    else:
        # Empty placeholder — user edits before first push.
        dest_md.write_text('')

    state = SessionState.new(
        doc_path=f'{slug}.md',
        channel_prefix=channel_prefix,
        platform=platform,
    )
    mirror.init_repo(target)
    # Save state.json BEFORE anything that can fail (gist creation, network,
    # `gh` auth). If a subsequent step blows up, the target dir has a
    # detectable "partial init" marker (state.json with gist_id=None) that
    # a re-run of `thrds <platform> init` picks up as resumable.
    state.save(target)

    if no_gist:
        mirror.commit(target, [f'{slug}.md', str(STATE_PATH)], f'thrds: init {slug}')
    else:
        # Gist-mirrored path: create gist FIRST (it seeds an initial commit
        # from the doc), align local HEAD to that commit, then re-save
        # state.json (now with gist_id) as our fast-forward commit on top.
        # This way local and gist share the same history from commit 1.
        _do_gist_init(target, state, slug)

    return target, state, False


def _print_init_completion(target: Path, state: SessionState, extra_hint: str | None = None) -> None:
    """Standard end-of-fresh-init stderr hints: session id, optional
    platform-specific line, cd hint. Not called on resume."""
    err(f"Initialized session {state.session_id} at {target}")
    if extra_hint:
        err(extra_hint)
    try:
        rel = target.relative_to(Path.cwd())
        err(f"cd {rel} to work on this doc")
    except ValueError:
        # target isn't under cwd (git-root was above cwd). Print absolute.
        err(f"cd {target} to work on this doc")


def _scan_bounds(
    cursor: str | None,
    oldest_days: float | None,
    latest_days: float | None,
    max_pages: int,
) -> tuple[float | None, float | None, str | None, int | None]:
    """Validate + resolve the scan-window CLI flags.

    Returns ``(oldest_ts, latest_ts, cursor, effective_max_pages)`` — days
    are converted to unix timestamps against `time.time()` at call time,
    `max_pages=0` maps to `None` (uncapped). Raises `UsageError` on invalid
    combinations (currently just `--cursor` + `--latest-days`).
    """
    if cursor is not None and latest_days is not None:
        raise click.UsageError('--cursor and --latest-days are mutually exclusive (both specify where to start).')
    now = time.time()
    oldest_ts = now - oldest_days * 86400 if oldest_days is not None else None
    latest_ts = now - latest_days * 86400 if latest_days is not None else None
    return oldest_ts, latest_ts, cursor, (max_pages if max_pages > 0 else None)


def _scan_sessions(
    channel: str,
    oldest_ts: float | None,
    latest_ts: float | None,
    cursor: str | None,
    max_pages: int | None,
) -> 'tuple[dict[str, object], str]':
    """Run `scan_thrds_metadata` with a progress log; translate
    `ScanCapReached` to a `UsageError` after surfacing the resume cursor
    on its own stderr line. Shared between `recover` and `list-sessions`.

    Returns ``(sessions, resolved_channel_id)`` — resolving the channel
    name up front means downstream code (state writeback, doc pull) all
    use the ID consistently.
    """
    client = _make_slack_client()
    channel = _resolve_channel(client, channel)
    total_msgs = 0

    def on_page(page_num: int, msg_count: int) -> None:
        nonlocal total_msgs
        total_msgs += msg_count
        err(f'  scan page {page_num}: {msg_count} msgs (total {total_msgs})')

    try:
        sessions = client.scan_thrds_metadata(
            channel,
            oldest=oldest_ts,
            latest=latest_ts,
            cursor=cursor,
            max_pages=max_pages,
            on_page=on_page,
        )
    except ScanCapReached as e:
        # Cursor on its own line — easy to grep out of the surrounding text.
        if e.next_cursor:
            err(f'  next cursor: {e.next_cursor}')
        raise click.UsageError(str(e))
    return sessions, channel


def _print_sessions_table(sessions: dict, channel: str) -> None:
    """Emit a newest-first table of `{sid: RecoveredSession}` to stderr.

    Header + rows aligned by fixed-width columns. Used by both the
    `list-sessions` command (unconditionally) and `recover` (when >1
    session in the channel forces disambiguation).
    """
    err(f'  {"session_id":<40}  {"doc_slug":<24}  threads  newest_ts')
    for sid, sess in sorted(sessions.items(), key=lambda kv: float(kv[1].newest_ts), reverse=True):
        err(f'  {sid:<40}  {sess.doc_slug:<24}  {sess.thread_count:>7}  {sess.newest_ts}')


def _resolve_channel(client, ref: str) -> str:
    """Resolve a channel reference to a Slack channel ID (``C…``).

    Accepts:
    - Bare Slack ID (``C…`` / ``G…``): passthrough. Detection: first char
      is uppercase — Slack channel names are always lowercase, so an
      uppercase-starting ref cannot be a name. Cheap + unambiguous.
    - ``#name`` or ``name``: looked up via
      :meth:`SlackClient.list_channels_by_name`, case-insensitive.

    Raises :class:`click.UsageError` on missing scope (``channels:read``
    or ``groups:read``) or unknown name — with a hint listing the first
    few names the token *can* see.
    """
    if ref and ref[0].isupper():
        return ref
    name = ref.lstrip('#').lower()
    try:
        channels = client.list_channels_by_name()
    except RuntimeError as e:
        if 'missing_scope' in str(e):
            raise click.UsageError(
                f'Channel-name lookup requires `channels:read` (public) or '
                f'`groups:read` (private) scope on the token. Pass a Slack ID '
                f'(`C…`) instead, or add the scope. (Slack error: {e})'
            )
        raise
    cid = channels.get(name)
    if cid is None:
        preview = sorted(channels)[:5]
        raise click.UsageError(
            f'Channel {ref!r} not found. Pass a Slack ID (C…) or an exact '
            f'name (with or without leading #). Available (first 5 of '
            f'{len(channels)}): {preview}'
        )
    return cid


def _channel_url(channel_id: str) -> str:
    """Build the Slack workspace URL for a channel from its ID.

    Uses the auth'd workspace (via `auth.test`) so the URL points at the
    same workspace the token belongs to. The web URL takes a channel ID as
    the path segment and Slack redirects appropriately.
    """
    client = _make_slack_client()
    # `auth.test` returns `url`: `https://<workspace>.slack.com/`.
    workspace_url = client._request('auth.test', {}, method='GET')['url'].rstrip('/')
    return f'{workspace_url}/archives/{channel_id}'


# --- Low-level Slack CRUD helpers (shared by `slack {history,thread,rm,post,edit,permalink}`) ---
#
# Motivation + full design in `specs/done/slack-crud-cli.md`. Six verbs that
# wrap `SlackClient` primitives one-to-one for scripting and ad-hoc CRUD, so
# throwaway `_request` heredocs stop reappearing. Deliberate divergence from
# the session verbs: `post`/`edit` default to **raw mrkdwn** (no `to_slack()`
# md→mrkdwn conversion), with `-m/--markdown` to opt into the session-verb
# behavior. See `specs/done/raw-mrkdwn-passthrough.md` for the underlying
# `raw=` kwarg.

_CRUD_TS_MAX_TEXT = 100


def _crud_client(channel_ref: str) -> SlackClient:
    """Build a `SlackClient` with `self.channel` resolved from ``channel_ref``.

    Every CRUD verb ends up making at least one call that reads
    ``self.channel`` (delete / post / edit / permalink); centralize the
    resolve + assignment so each verb stays a one-liner.
    """
    client = _make_slack_client()
    client.channel = _resolve_channel(client, channel_ref)
    return client


def _crud_sender(msg: dict) -> str:
    """Compact "sender" column for `slack history` / `slack thread` display.

    Preference order: explicit ``username`` (per-message override, e.g. a
    ``chat.postMessage`` with ``username=…``) → ``user`` (Slack user ID
    ``U…``) → ``bot_id`` (``B…`` for bot-authored posts). Falls back to
    ``?`` if none present (should be rare). No `users.info` resolution
    here — the CRUD CLI is a low-level view; ``-j/--json`` gives the full
    dict if the caller wants the resolved name.
    """
    return (
        msg.get("username")
        or msg.get("user")
        or msg.get("bot_id")
        or "?"
    )


def _crud_first_line(text: str) -> str:
    """Return the first non-empty line of ``text`` truncated to a display width.

    The ``history`` / ``thread`` tables show one line per message; long
    messages get elided with ``…`` at ``_CRUD_TS_MAX_TEXT`` chars. Empty
    messages (image-only / edited-to-empty) render as an empty string
    so the row still lines up.
    """
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped:
            if len(stripped) > _CRUD_TS_MAX_TEXT:
                return stripped[: _CRUD_TS_MAX_TEXT - 1] + "…"
            return stripped
    return ""


def _crud_render_messages(msgs: list[dict]) -> str:
    """Render a table of ``ts  sender  text`` lines from raw Slack msg dicts.

    Sender column is left-padded to the max sender width seen this call,
    so rows align without depending on a fixed magic number (channels
    with only long custom sender names look OK; bot-heavy channels stay
    tight).
    """
    if not msgs:
        return ""
    senders = [_crud_sender(m) for m in msgs]
    sender_w = max(len(s) for s in senders)
    lines = []
    for m, sender in zip(msgs, senders):
        ts = m.get("ts", "?")
        text = _crud_first_line(m.get("text", "") or "")
        lines.append(f"{ts}  {sender:<{sender_w}}  {text}")
    return "\n".join(lines)


@click.group()
def cli():
    """`thrds`: draft multi-thread posts locally; sync to Slack, or capture-only.

    Subgroups: ``slack`` (Slack session + CRUD verbs), ``capture`` (gist-only
    trajectory, no platform target). Every session's platform is stamped
    into `thrds.json` at init and enforced on subsequent verbs.
    """
    pass


# ============================================================================
# `thrds slack …` subgroup — session verbs + low-level CRUD.
# ============================================================================


@cli.group("slack")
def slack_cli():
    """Slack workflow: `init`/`push`/`pull`/`diff`/`archive`/`open`/`list-sessions`/`recover`
    plus low-level CRUD (`history`/`thread`/`rm`/`post`/`edit`/`permalink`).

    Session verbs draft locally in `.md`, sync to a staging PC in Slack, let
    you tweak in-Slack, pull back. CRUD verbs wrap `SlackClient` primitives
    one-to-one (post/edit default to raw mrkdwn; pass `-m` to opt into local
    md → mrkdwn conversion — reverse of the session verbs' default).
    """
    pass


@slack_cli.command("init")
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.option('-p', '--prefix', help='Staging PC channel-name prefix override (else `THRDS_CHANNEL_PREFIX` env, else empty).')
@click.argument('doc_path')
def slack_init(no_gist: bool, prefix: str | None, doc_path: str):
    """Initialize a `thrds slack` session for DOC_PATH (a `.md` file).

    Creates ``<git-root-or-cwd>/thrds/<slug>/`` (ghpr-style), copies or
    creates the doc there, `git init`s the session dir, and by default
    creates a secret gist and adds it as the ``g`` remote.

    Auto-resume: if the target dir already exists and holds a valid
    ``thrds.json`` (matching platform) with ``gist_id: null``, finishes
    the gist-creation step instead of refusing (recovers from an earlier
    init that failed at the gist step — e.g. network hiccup, `gh` unauth'd).
    """
    target, state, was_resume = _do_init(doc_path, no_gist, prefix, platform='slack')
    if was_resume:
        return
    _print_init_completion(
        target, state,
        extra_hint=f"Staging PC name (on first push): {state.staging_channel_name()}",
    )


@slack_cli.command("push")
@click.option('-c', '--channel', help='Prod channel override (legacy sessions only).')
@click.option('-k', '--keep-staging', is_flag=True, help='Do not auto-archive the staging PC after --prod (legacy).')
@click.option('-n', '--dry-run', is_flag=True, help='Show plan without side effects.')
@click.option('-p', '--prod', is_flag=True, help='Push to prod (legacy sessions only; use `promote` instead).')
@click.argument('doc_path', required=False)
def slack_push(channel: str | None, keep_staging: bool, dry_run: bool, prod: bool, doc_path: str | None):
    """Push this session's threads to the staging PC (terraform).

    On a per-thread session, pushes every `NN-slug.md` and takes no `--prod`:
    promoting to a real channel is per-thread and deliberate, via
    `thrds slack promote <slug>`.

    `--prod` remains only for legacy single-doc sessions that haven't been
    through `thrds slack migrate` yet.
    """
    state = _load_state(expected_platform='slack')
    client = _make_slack_client()

    if not state.is_legacy:
        if prod or channel is not None or keep_staging:
            raise click.UsageError(
                'Per-thread sessions push only to staging; use '
                '`thrds slack promote <slug>` to post a thread to its target.'
            )
        files = thread_files(Path.cwd())
        threads = read_threads(Path.cwd())
        if not threads:
            raise click.UsageError('No thread files (`NN-slug.md`) in this session.')
        for slug, target in _absorb_file_chrome(Path.cwd(), threads, state, client, dry_run).items():
            where = target.channel + (f' @ {target.thread_ts}' if target.thread_ts else '')
            err(f"targeted {slug} → {where} (from a chrome line in its file)")
        result = client.sync_threads_staging(
            threads, state, dry_run=dry_run,
            filenames={f.slug: f.name for f in files},
        )
        _print_sync_summary('pushed to staging' + (' (dry-run)' if dry_run else ''), result)
        if not dry_run:
            paths = [f.name for f in thread_files(Path.cwd())] + [str(STATE_PATH)]
            _autocommit(Path.cwd(), paths, 'thrds: push staging')
        return

    doc = _load_doc(state, doc_path)
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    if not prod and keep_staging:
        raise click.UsageError('--keep-staging requires --prod.')
    if channel is not None:
        channel = _resolve_channel(client, channel)
    if prod:
        result = client.sync_doc_prod(
            doc, state,
            channel=channel,
            keep_staging=keep_staging,
            dry_run=dry_run,
        )
        mode = 'prod'
    else:
        result = client.sync_doc_staging(doc, state, dry_run=dry_run)
        mode = 'staging'
    _print_sync_summary(f'pushed to {mode}' + (' (dry-run)' if dry_run else ''), result)
    if prod and not dry_run and not keep_staging and state.staging_channel is not None:
        err(f"  archived staging PC: {state.staging_channel}")
    if not dry_run:
        doc_rel = _resolve_doc_path(state, doc_path)
        _autocommit(
            Path.cwd(),
            [doc_rel, str(STATE_PATH)],
            f'thrds: push {mode}',
        )


@slack_cli.command("pull")
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-n', '--dry-run', is_flag=True, help='Print the pulled state to stdout instead of writing files.')
@click.option('-p', '--prod', is_flag=True, help='Pull from prod channel.')
@click.argument('doc_path', required=False)
def slack_pull(channel: str | None, dry_run: bool, prod: bool, doc_path: str | None):
    """Pull the doc's current state from Slack. Default: staging PC.

    Writes each pulled thread back to its file and (if the session is a git
    repo) auto-commits + pushes to the gist mirror — the mirror image of
    ``push``, which syncs by default too. With ``--dry-run`` the pulled state
    is printed to stdout (frontmatter omitted) and nothing is written.
    """
    write = not dry_run
    state = _load_state(expected_platform='slack')
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    client = _make_slack_client()
    if channel is not None:
        channel = _resolve_channel(client, channel)
    session_dir = Path.cwd()

    if not state.is_legacy:
        if prod:
            raise click.UsageError(
                'Per-thread sessions pull from staging only; a promoted thread '
                'is tracked by its `posted_ts`.'
            )
        threads = client.pull_threads_staging(state, session_dir=session_dir)
        files = {f.slug: f.name for f in thread_files(session_dir)}
        renames: list[tuple[Path, Path]] = []
        for slug, edit in client.pull_chrome_edits(state, files).items():
            if edit.target_now is not None:
                where = edit.target_now.channel + (
                    f' @ {edit.target_now.thread_ts}' if edit.target_now.thread_ts else ''
                )
                err(f"retargeted {slug} → {where} (edited in Slack)")
            if edit.renamed_to is not None:
                renames.append((session_dir / files[slug], session_dir / edit.renamed_to))
                err(f"renamed {files[slug]} → {edit.renamed_to} (edited in Slack)")

        adopted = client.adopt_new_staging_threads(state, session_dir)
        for a in adopted:
            err(f"adopted {a.filename} (written straight into staging)")

        by_slug = {t.slug: t for t in threads}
        by_slug.update({a.slug: a.thread for a in adopted})
        if write:
            for src, dst in renames:
                src.rename(dst)
            for a in adopted:
                (session_dir / a.filename).touch()

        written: list[str] = []
        for tf in thread_files(session_dir):
            thread = by_slug.get(tf.slug)
            if thread is None:
                continue
            text = serialize_thread(thread)
            if write:
                tf.path.write_text(text)
                written.append(tf.name)
            else:
                click.echo(f'--- {tf.name} ---', err=True)
                click.echo(text, nl=False)
        if write:
            state.save()
            err(f"wrote {len(written)} thread file(s): {', '.join(written)}")
            emoji_paths = [p.name for p in session_dir.glob('emoji-*') if p.is_file()]
            removed = [src.name for src, _ in renames]
            _autocommit(
                session_dir,
                [*written, *removed, str(STATE_PATH), *emoji_paths],
                'thrds: pull staging',
            )
        return

    doc = (
        client.pull_doc_prod(state, channel=channel, session_dir=session_dir)
        if prod
        else client.pull_doc_staging(state, session_dir=session_dir)
    )
    text = serialize_doc(doc)
    if write:
        path = _resolve_doc_path(state, doc_path)
        Path(path).write_text(text)
        # Persist any new custom emoji entries pulled into state.workspace_emoji.
        state.save()
        err(f"wrote pulled doc to {path}")
        mode = 'prod' if prod else 'staging'
        # Emoji files (`emoji-*.<ext>`) newly-downloaded by pull_doc_* land at
        # session root; add them to the auto-commit so the gist mirror carries them.
        emoji_paths = [p.name for p in session_dir.glob('emoji-*') if p.is_file()]
        _autocommit(session_dir, [path, str(STATE_PATH), *emoji_paths], f'thrds: pull {mode} → {path}')
    else:
        click.echo(text, nl=False)


def _thread_slug_arg(arg: str) -> str:
    """The slug an argument names, accepting a filename as well as a bare slug.

    ``diff 05-grug.md`` is what shell completion produces and what anyone
    looking at the directory listing types, so both spellings resolve.
    """
    name = Path(arg).name
    parsed = parse_thread_filename(name if name.endswith('.md') else f'{name}.md')
    return parsed[1] if parsed is not None else name


def _diff_threads(
    client: SlackClient,
    state: SessionState,
    session_dir: Path,
    slug_arg: str | None,
) -> None:
    """Print each staged thread's local↔Slack diff; nothing for unchanged ones."""
    files = {tf.slug: tf for tf in thread_files(session_dir)}
    staged = {slug for slug, e in state.threads.items() if e.staging_ts is not None}

    if slug_arg is not None:
        slug = _thread_slug_arg(slug_arg)
        if slug not in files and slug not in staged:
            available = ', '.join(sorted(files)) or '(none)'
            raise click.UsageError(
                f"No thread {slug!r} in this session; available: {available}"
            )
        if slug not in staged:
            err(f"{slug}: never pushed to staging — nothing on the Slack side to compare.")
            return
        wanted = [slug]
    else:
        # File order first (the NN prefix is post order), then any thread that
        # exists in state but has no file — a locally-deleted draft still has a
        # Slack side, and "pull would restore it" is exactly what diff reports.
        wanted = [tf.slug for tf in sorted(files.values()) if tf.slug in staged]
        wanted += sorted(staged - set(files))
        if not wanted:
            err('No staged threads to diff.')
            return

    threads = {
        t.slug: t
        for t in client.pull_threads_staging(state, session_dir=session_dir, slugs=wanted)
    }
    for slug in wanted:
        tf = files.get(slug)
        name = tf.name if tf is not None else f'{slug}.md'
        local = tf.path.read_text() if tf is not None else ''
        thread = threads.get(slug)
        # A thread whose OP was deleted in Slack comes back with no messages;
        # `serialize_thread` of that is a lone newline, which would read as
        # "one blank line remains" rather than "it's gone".
        remote = serialize_thread(thread) if thread is not None and thread.messages else ''
        click.echo(
            diff_texts(local, remote, f'{name} (local)', f'{name} (slack)'),
            nl=False,
        )


@slack_cli.command("diff")
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-p', '--prod', is_flag=True, help='Diff against prod channel (legacy sessions only).')
@click.argument('target', required=False)
def slack_diff(channel: str | None, prod: bool, target: str | None):
    """Show what `pull` would change: local content vs. Slack's current state.

    Per-thread sessions diff each thread that has a `staging_ts` against its
    own file, emitting one unified diff per changed thread and nothing for
    unchanged ones. TARGET restricts that to a single thread (slug or
    filename). Always exits 0 — this is a report, not a gate.

    The local side is the working-tree file verbatim, not its canonical
    re-serialization, because `pull` overwrites the file: local formatting
    that doesn't survive a round trip *is* a pending change, and canonicalizing
    first would hide it.

    Legacy single-doc sessions diff DOC_PATH against the whole staging (or,
    with `--prod`, prod) doc.
    """
    state = _load_state(expected_platform='slack')
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    client = _make_slack_client()
    if channel is not None:
        channel = _resolve_channel(client, channel)
    session_dir = Path.cwd()

    if not state.is_legacy:
        if prod:
            raise click.UsageError(
                'Per-thread sessions diff against staging only; a promoted thread '
                'is tracked by its `posted_ts`.'
            )
        _diff_threads(client, state, session_dir, target)
        return

    local_doc = _load_doc(state, target)
    slack_doc = (
        client.pull_doc_prod(state, channel=channel, session_dir=session_dir)
        if prod
        else client.pull_doc_staging(state, session_dir=session_dir)
    )
    click.echo(diff_docs(local_doc, slack_doc, from_label='local', to_label='slack'), nl=False)


@slack_cli.command("archive")
@click.option('-f', '--force', is_flag=True, help='Archive even with threads still pending.')
def slack_archive(force: bool):
    """Archive this session's staging PC (reversible via Slack UI/API `unarchive`).

    Refuses while any thread is still `draft` or `ready` — tearing down the
    scratchpad with live drafts in it is exactly the failure the per-thread
    model exists to prevent, and it's why prod push no longer auto-archives.
    Mark stragglers with `thrds slack drop <slug>`, or pass `-f`.

    Idempotent — subsequent invocations no-op via the ``state.staging_archived``
    flag. Auto-commits + pushes the flag change to the gist mirror.
    """
    state = _load_state(expected_platform='slack')
    if state.staging_channel is None:
        err("No staging PC to archive.")
        return
    if state.staging_archived:
        err(f"Already archived: {state.staging_channel}")
        return
    pending = state.pending_threads()
    if pending and not force:
        raise click.UsageError(
            f"{len(pending)} thread(s) still pending: {', '.join(pending)}. "
            f"Promote or drop them first, or pass -f to archive anyway."
        )
    client = _make_slack_client()
    client.archive_channel(state.staging_channel)
    state.staging_archived = True
    state.save()
    err(f"archived staging PC: {state.staging_channel}")
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: archive {state.staging_channel}')


def _require_per_thread(state: SessionState) -> None:
    """Reject a legacy session on a per-thread verb, pointing at `migrate`."""
    if state.is_legacy:
        raise click.UsageError(
            'This session is still on the legacy single-doc layout; '
            'run `thrds slack migrate` first.'
        )


@slack_cli.command("promote")
@click.option('-c', '--channel', help='Override the thread\'s recorded target channel.')
@click.option('-n', '--dry-run', is_flag=True, help='Resolve and render; post nothing.')
@click.option('-t', '--thread-ts', help='Post as a reply into this existing thread.')
@click.option('-y', '--yes', is_flag=True, help='Skip the confirmation prompt.')
@click.argument('slug')
def slack_promote(channel: str | None, dry_run: bool, thread_ts: str | None, yes: bool, slug: str):
    """Post a single thread (SLUG) to its target channel.

    The per-thread replacement for the old `push --prod`, which fired the
    *whole doc* at one channel and then archived the staging PC — wrong when
    the session holds several drafts at different readiness, bound for
    different places (see ``specs/per-thread-model.md``).

    Resolves the destination from the thread's own metadata (falling back to
    the session's `prod_channel`), prints it alongside the exact body that
    will be posted, and asks before sending. Posts only this thread, and
    never archives the staging channel — other drafts are still live.

    With a target `thread_ts` (recorded, or passed via `-t`), the messages go
    into that existing thread as replies; the other person's messages are
    left untouched.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)

    try:
        _, thread = find_thread(Path.cwd(), slug)
    except ValueError as e:
        raise click.UsageError(str(e))

    target = state.target_for(slug)
    if channel is not None:
        target = ThreadTarget(channel=channel, thread_ts=thread_ts or (target.thread_ts if target else None))
    elif thread_ts is not None:
        if target is None:
            raise click.UsageError(
                f'No target channel for {slug!r} — pass --channel alongside --thread-ts.'
            )
        target = ThreadTarget(channel=target.channel, thread_ts=thread_ts)
    if target is None:
        raise click.UsageError(
            f'No target for thread {slug!r} — pass --channel, or set the session\'s '
            f'prod_channel, before promoting.'
        )

    client = _make_slack_client()
    resolved = _resolve_channel(client, target.channel)
    target = ThreadTarget(channel=resolved, thread_ts=target.thread_ts)

    entry = state.thread(slug)
    kind = 'reply into' if target.thread_ts else 'new thread in'
    err(f"promote {slug} → {kind} {target.channel}"
        + (f" @ {target.thread_ts}" if target.thread_ts else ''))
    if entry.state == 'posted':
        err(f"  note: already posted (ts {entry.posted_ts}) — this will sync it in place")
    err('  ---')
    for i, m in enumerate(m for m in thread.messages if m.author is None):
        prefix = '  OP   ' if i == 0 else f'  +{i:<4d}'
        for j, line in enumerate(m.content.split('\n')):
            err(f"{prefix if j == 0 else '       '} {line}")
    err('  ---')

    if dry_run:
        err('(dry run — nothing posted)')
        return
    if not yes:
        click.confirm('Post it?', abort=True, err=True)

    result = client.promote_thread(slug, thread, target, state)
    state.save()
    err(f"posted {slug}: {result.thread_id}")
    _notify_promoted(client, slug, target.channel, result.thread_id)
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: promote {slug} → {target.channel}')


@slack_cli.command("replay")
@click.option('-b', '--branch', default='per-thread', show_default=True, help='Branch to write the rewritten history to.')
@click.option('-n', '--dry-run', is_flag=True, help='Plan and verify; write no branch.')
@click.option('-r', '--ref', default='HEAD', show_default=True, help='Ref whose history to rewrite.')
def slack_replay(branch: str, dry_run: bool, ref: str):
    """Rewrite this session's git history into the per-thread layout.

    `migrate` converts the working tree; `replay` converts the *history*, so a
    session collected as a writing example can be read without learning the
    retired `===` syntax. Every commit is rebuilt with the doc split into
    `NN-slug.md` files, preserving message, author, committer, and dates.

    Indices are assigned globally, from the final commit's ordering — a slug
    keeps one number for all time, and a commit where the thread doesn't exist
    yet just has a gap. Numbering each commit independently would renumber
    every thread below an insertion, and a rename breaks the per-file history
    this layout exists to produce.

    Verifies before writing: for every commit, the new files must parse back to
    exactly the threads the old doc parsed to. Writes a new branch and never
    force-pushes — inspect it, then move the remote yourself.
    """
    state = _load_state(expected_platform='slack')
    doc_path = state.doc_path
    if doc_path is None:
        raise click.UsageError(
            'This session has no `doc_path` — its history is already per-thread, '
            'or it was migrated before replaying (replay reads the legacy doc).'
        )
    repo = Path.cwd()
    try:
        plans, index_by_slug = plan_replay(repo, ref, doc_path)
        problems = verify_plan(repo, plans, doc_path)
    except ReplayError as e:
        raise click.UsageError(str(e))

    err(f'replay {ref}: {len(plans)} commit(s), {len(index_by_slug) - 1} thread(s)')
    for slug, i in sorted(index_by_slug.items(), key=lambda kv: kv[1]):
        err(f'  {thread_filename(i, slug)}')
    for plan in plans:
        err(f'  {plan.sha[:8]} {plan.subject[:48]:<48s} {len(plan.md_names)} file(s)')

    if problems:
        err('verification FAILED:')
        for p in problems:
            err(f'  {p}')
        raise click.UsageError('Refusing to write a rewrite that loses content.')
    err('verification: every commit round-trips identically')

    if dry_run:
        err('(dry run — no branch written)')
        return
    head = write_replay(repo, plans, branch)
    err(f'wrote {branch} → {head[:8]}')
    err(f'inspect with: git log --stat {branch}')


@slack_cli.command("adopt")
@click.option('-c', '--channel', required=True, help='Channel the message already lives in.')
@click.option('-t', '--ts', required=True, help='Timestamp of the existing prod message.')
@click.option('-T', '--in-thread', help='Parent thread ts, if the message is a reply.')
@click.option('-V', '--no-verify', is_flag=True, help='Skip the permalink check on TS.')
@click.argument('slug')
def slack_adopt(channel: str, ts: str, in_thread: str | None, no_verify: bool, slug: str):
    """Record an existing prod message as SLUG's posted result, without posting.

    For threads that went out before thrds was tracking them — posted by hand,
    or by an older version that never recorded `prod_threads`. Marks the thread
    `posted`, pins its target, and stores the message ts, so `status` and the
    archive gate see reality.

    Posts nothing. By default resolves the ts to a permalink first, since a
    mistyped ts would otherwise be recorded silently and only surface later as
    a `promote` syncing against a message that doesn't exist. That permalink is
    also kept (as `posted_url`), so staging chrome and `status` can link to the
    real message without another API call; `-V` skips the check and therefore
    records no URL.

    `-T` records that the message is a *reply* in an existing thread — the
    "considered response to someone else's post" case, where TS is our message
    and `-T` is the message we answered. Omitted, an existing target's parent
    thread in the same channel is kept rather than silently flattened to
    top-level.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)
    try:
        find_thread(Path.cwd(), slug)
    except ValueError as e:
        raise click.UsageError(str(e))

    client = _make_slack_client()
    resolved = _resolve_channel(client, channel)
    entry = state.thread(slug)
    if entry.state == 'posted':
        err(f"note: {slug} was already posted (ts {entry.posted_ts}); overwriting")

    link = None
    if not no_verify:
        prev, client.channel = client.channel, resolved
        try:
            link = client.permalink(ts)
        except Exception as e:  # noqa: BLE001 — surface the API's own complaint
            raise click.UsageError(f"Could not resolve {ts} in {resolved}: {e}")
        finally:
            client.channel = prev
        err(f"  {link}")

    parent = in_thread
    if parent is None and entry.target is not None and entry.target.channel == resolved:
        parent = entry.target.thread_ts
    entry.target = ThreadTarget(channel=resolved, thread_ts=parent)
    entry.posted_ts = ts
    entry.posted_url = link
    entry.state = 'posted'
    state.save()
    err(f"adopted {slug} → {resolved} @ {ts}"
        + (f" (in thread {parent})" if parent else ''))
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: adopt {slug} → {resolved}')


@slack_cli.command("reorder")
@click.option('-n', '--dry-run', is_flag=True, help='Show the renames; touch nothing.')
@click.argument('slugs', nargs=-1)
def slack_reorder(dry_run: bool, slugs: tuple[str, ...]):
    """Renumber thread files to a gapless `01..NN` in their current order.

    Pass SLUGS to put those threads first, in that order; anything unnamed
    keeps its relative position after them. With no SLUGS this just compacts
    gaps left by adding and dropping threads.

    Only files move. `thrds.json` is keyed by slug, not by index, so the
    staging messages, targets and posted timestamps all follow their thread
    without being touched — the number says where a thread sorts, nothing more.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)
    session_dir = Path.cwd()
    files = thread_files(session_dir)
    if not files:
        err('No thread files in this session.')
        return

    by_slug = {f.slug: f for f in files}
    unknown = [s for s in slugs if s not in by_slug]
    if unknown:
        raise click.UsageError(
            f"No thread(s) {', '.join(unknown)} in this session; "
            f"available: {', '.join(f.slug for f in files)}"
        )
    named = list(dict.fromkeys(slugs))
    order = [by_slug[s] for s in named] + [f for f in files if f.slug not in set(named)]

    moves = [
        (f, thread_filename(i, f.slug))
        for i, f in enumerate(order, start=1)
        if f.name != thread_filename(i, f.slug)
    ]
    if not moves:
        err('Already gapless and in order; nothing to do.')
        return
    for f, new_name in moves:
        err(f"  {f.name} → {new_name}")
    if dry_run:
        err('(dry run — nothing renamed)')
        return

    # Two-phase, because a reorder is a permutation: renaming 01→02 directly
    # would clobber the file already at 02.
    staged: list[tuple[Path, Path]] = []
    for f, new_name in moves:
        tmp = session_dir / f'.{f.name}.reorder'
        f.path.rename(tmp)
        staged.append((tmp, session_dir / new_name))
    for tmp, dst in staged:
        tmp.rename(dst)

    err(f"renumbered {len(moves)} file(s)")
    _autocommit(
        session_dir,
        [f.name for f, _ in moves] + [n for _, n in moves],
        'thrds: reorder thread files',
    )


@slack_cli.command("drop")
@click.argument('slug')
def slack_drop(slug: str):
    """Mark thread SLUG as abandoned (`dropped`) without posting it.

    The other terminal state besides `posted`. Exists so the archive gate has
    a way to say "this draft is finished business" about a thread that is
    never going out — without deleting the file, since the trajectory of a
    draft that got abandoned is still part of the record.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)
    entry = state.thread(slug)
    if entry.state == 'posted':
        raise click.UsageError(
            f"Thread {slug!r} was already posted (ts {entry.posted_ts}); refusing to mark it dropped."
        )
    entry.state = 'dropped'
    state.save()
    err(f"dropped {slug}")
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: drop {slug}')


@slack_cli.command("reopen")
@click.option('-s', '--state', 'to_state', default='draft', show_default=True,
              type=click.Choice(['draft', 'ready']), help='State to return the thread to.')
@click.argument('slug')
def slack_reopen(to_state: str, slug: str):
    """Move a terminal thread (SLUG) back to `draft`/`ready` so it can be revised.

    The counterpart to finalizing: once a thread is `posted` or `dropped` its
    staged copy re-renders with chrome in a block, which Slack makes
    uneditable. That's the signal, and this is the way out of it — the next
    `push` unlocks the staged message.

    Keeps `posted_ts` and the target: reopening says "I want to revise this",
    not "this never happened". A subsequent `promote` therefore syncs the
    already-posted message in place rather than posting a second one.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)
    entry = state.thread(slug)
    if not entry.is_terminal:
        err(f"{slug} is already {entry.state}; nothing to reopen.")
        return
    was = entry.state
    entry.state = to_state
    state.save()
    err(f"reopened {slug}: {was} → {to_state}"
        + (f" (still posted at {entry.posted_ts})" if entry.posted_ts else ''))
    err('  run `thrds slack push` to unlock its staged copy')
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: reopen {slug}')


@slack_cli.command("status")
def slack_status():
    """List this session's threads: file, state, destination, posted permalink.

    Four tab-separated columns, always — the last is empty for anything not
    posted, so `cut -f4` stays honest.
    """
    state = _load_state(expected_platform='slack')
    _require_per_thread(state)
    files = thread_files(Path.cwd())
    if not files:
        err('No thread files in this session.')
        return
    for tf in files:
        entry = state.threads.get(tf.slug)
        st = entry.state if entry is not None else 'draft'
        target = state.target_for(tf.slug)
        if target is None:
            dest = '(no target)'
        elif target.thread_ts:
            dest = f'{target.channel} @ {target.thread_ts}'
        else:
            dest = target.channel
        url = entry.posted_url if entry is not None and entry.posted_url else ''
        click.echo(f'{tf.name}\t{st}\t{dest}\t{url}')


@slack_cli.command("migrate")
@click.option('-n', '--dry-run', is_flag=True, help='Print the plan; write nothing.')
def slack_migrate(dry_run: bool):
    """Split this session's single doc into per-thread `NN-slug.md` files.

    Converts a legacy one-doc-many-threads session to the per-thread model
    (see ``specs/per-thread-model.md``): each ``=== slug`` section becomes its
    own file, and `thrds.json` gains a `threads` map carrying each thread's
    staging ts, destination, and state. A preamble becomes ``00-preamble.md``.

    The payoff is per-file git history — a commit reads as "*this message*
    went v2→v3" rather than "the doc changed", which is what makes a session's
    gist usable as a revision trajectory.

    Content is preserved verbatim; only its distribution across files changes.
    Threads already posted to prod migrate as ``posted`` with their actual
    channel pinned; everything else stays ``draft`` and inherits the
    session-level ``prod_channel`` as a default.
    """
    state = _load_state(expected_platform='slack')
    if not state.is_legacy:
        if state.threads:
            raise click.UsageError(
                'This session is already on the per-thread model '
                f'({len(state.threads)} thread(s) in thrds.json).'
            )
        raise click.UsageError('Nothing to migrate: this session has no threads recorded.')

    doc_path = _resolve_doc_path(state, None)
    parsed = parse_doc(Path(doc_path).read_text())
    try:
        plan = plan_migration(parsed.doc, state, doc_path, parsed.frontmatter)
    except ValueError as e:
        raise click.UsageError(str(e))

    err(f"migrate {doc_path} → {len(plan.threads)} thread file(s):")
    for t in plan.threads:
        target = t.entry.target
        dest = f" → {target.channel}" if target is not None else ''
        if target is not None and target.thread_ts is not None:
            dest += f" (reply to {target.thread_ts})"
        err(f"  {t.filename}  [{t.entry.state}]{dest}")

    if dry_run:
        err('(dry run — nothing written)')
        return

    try:
        touched = apply_migration(Path.cwd(), state, plan)
    except ValueError as e:
        raise click.UsageError(str(e))
    state.save()
    err(f"removed {doc_path}; wrote {len(plan.threads)} file(s)")
    _autocommit(
        Path.cwd(),
        [p.name for p in touched] + [str(STATE_PATH)],
        f'thrds: migrate {doc_path} → per-thread files',
    )


@slack_cli.command("open")
@click.option('-p', '--prod', is_flag=True, help='Open the prod channel (default: gist).')
@click.option('-s', '--staging', is_flag=True, help='Open the staging PC (default: gist).')
@click.option('-U', '--no-open', is_flag=True, help='Print the URL, do not launch browser.')
def slack_open(prod: bool, staging: bool, no_open: bool):
    """Open this session's gist (default), staging PC, or prod channel in the browser.

    Mirrors ``ghpr open [-g]`` — with three targets instead of two, since a
    thrds session tracks a gist + a staging channel + (once pushed) a prod
    channel. Pass ``-U`` to just print the URL (useful in scripts).
    """
    if prod and staging:
        raise click.UsageError('--prod and --staging are mutually exclusive.')
    state = _load_state(expected_platform='slack')

    if staging:
        if state.staging_channel is None:
            raise click.UsageError(
                'No staging channel — run `thrds slack push` first to create one.'
            )
        url = _channel_url(state.staging_channel)
        label = f'staging PC {state.staging_channel}'
    elif prod:
        if state.prod_channel is None:
            raise click.UsageError(
                'No prod channel recorded on this session.'
            )
        url = _channel_url(state.prod_channel)
        label = f'prod channel {state.prod_channel}'
    else:
        if state.gist_id is None:
            raise click.UsageError(
                'No gist recorded — session was init\'d with --no-gist.'
            )
        url = f'https://gist.github.com/{state.gist_id}'
        label = f'gist {state.gist_id}'

    err(f'Opening {label}: {url}')
    if not no_open:
        webbrowser.open(url)


@slack_cli.command('list-sessions')
@click.option('-c', '--cursor', help='Slack pagination cursor to resume from.')
@click.option('-d', '--oldest-days', type=float, help='Only scan messages posted in the last N days.')
@click.option('-D', '--latest-days', type=float, help='Skip messages newer than N days ago.')
@click.option('-m', '--max-pages', type=int, default=50, show_default=True, help='Cap on `conversations.history` pages fetched. Pass 0 to disable.')
@click.argument('channel')
def slack_list_sessions(cursor, oldest_days, latest_days, max_pages, channel):
    """List thrds sessions found in CHANNEL by scanning message metadata.

    Same scan machinery as `thrds slack recover` (same window / cap / cursor
    flags), but read-only — never writes state or pulls the doc. Useful
    to figure out which session_id you actually want before running
    `slack recover -i <sid>`, or just to see what's in a channel.
    """
    oldest_ts, latest_ts, cur, cap = _scan_bounds(cursor, oldest_days, latest_days, max_pages)
    sessions, channel = _scan_sessions(channel, oldest_ts, latest_ts, cur, cap)
    if not sessions:
        err(f'No thrds-metadata sessions found in {channel}.')
        return
    err(f'{len(sessions)} session{"s" if len(sessions) != 1 else ""} in {channel}:')
    _print_sessions_table(sessions, channel)


@slack_cli.command("recover")
@click.option('-c', '--cursor', help='Slack pagination cursor to resume from (printed by a prior `ScanCapReached`).')
@click.option('-d', '--oldest-days', type=float, help='Only scan messages posted in the last N days (default: unbounded).')
@click.option('-D', '--latest-days', type=float, help='Skip messages newer than N days ago (Slack `latest` upper bound). Pair with `--oldest-days` to scan a window.')
@click.option('-i', '--session-id', help='Session ID to recover (required if channel holds >1 session).')
@click.option('-m', '--max-pages', type=int, default=50, show_default=True, help='Safety cap on `conversations.history` pages fetched (200 msgs/page). Pass 0 to disable.')
@click.option('-s', '--staging', is_flag=True, help='Route recovered pointers to staging_threads (default: prod_threads[channel]).')
@click.option('-W', '--no-write-doc', is_flag=True, help='Skip the doc-pull step; write thrds.json only.')
@click.argument('channel')
def slack_recover(
    cursor: str | None,
    oldest_days: float | None,
    latest_days: float | None,
    session_id: str | None,
    max_pages: int,
    staging: bool,
    no_write_doc: bool,
    channel: str,
):
    """Rebuild `thrds.json` (and DOC.md) from Slack metadata in CHANNEL.

    Every ``thrds`` post carries ``event_type='thrds'`` metadata (session_id,
    doc_slug, thread_slug, kind); ``recover`` scans CHANNEL for those tags
    and reassembles the local session state — the durability story for the
    write-through cache in ``thrds.json``.

    Run inside an empty session dir. Refuses to overwrite an existing
    ``thrds.json`` (that's a live session, not a recovery target).

    Bare invocation lists sessions found in the channel and exits (code 2)
    if there's more than one — pass ``-i/--session-id`` to pick one; a
    single-session channel auto-selects.

    Scan cost: Slack does not index message metadata, so scanning is the
    only path. Default cap is 50 pages (10k messages). For busy channels,
    narrow with ``-d/--oldest-days N`` — the cheapest lever.
    """
    session_dir = Path.cwd()
    if (session_dir / STATE_PATH).is_file():
        raise click.UsageError(
            f'{STATE_PATH} already exists in {session_dir} — refusing to overwrite. '
            'cd into a fresh session dir before running `thrds slack recover`.'
        )
    oldest_ts, latest_ts, cur, cap = _scan_bounds(cursor, oldest_days, latest_days, max_pages)
    sessions, channel = _scan_sessions(channel, oldest_ts, latest_ts, cur, cap)
    if not sessions:
        raise click.UsageError(
            f'No thrds-metadata messages found in {channel}. '
            "Either the channel has no thrds posts, or a different token/app "
            'made the posts (Slack only returns metadata to the posting app).'
        )
    if session_id is None:
        if len(sessions) == 1:
            session_id = next(iter(sessions))
            err(f'Auto-selecting the only session in {channel}: {session_id}')
        else:
            err(f'{len(sessions)} thrds sessions found in {channel}; pass -i/--session-id to pick one:')
            _print_sessions_table(sessions, channel)
            raise click.exceptions.Exit(2)
    if session_id not in sessions:
        raise click.UsageError(
            f'Session {session_id!r} not found in {channel}. '
            f'Available: {sorted(sessions)}'
        )
    recovered = sessions[session_id]
    # Assemble SessionState from the metadata trail. session_id is preserved
    # (not freshly minted) — that's the whole point of recovery. platform
    # defaults to 'slack' since this is `thrds slack recover`.
    state = SessionState(
        session_id=recovered.session_id,
        doc_path=f'{recovered.doc_slug}.md',
        platform='slack',
    )
    if staging:
        state.staging_channel = channel
        state.staging_preamble_ts = recovered.preamble_ts
        state.staging_threads = dict(recovered.thread_ts_by_slug)
    else:
        state.prod_channel = channel
        if recovered.preamble_ts is not None:
            state.prod_preamble_ts[channel] = recovered.preamble_ts
        state.prod_threads[channel] = dict(recovered.thread_ts_by_slug)
    state.save(session_dir)
    err(
        f'wrote {STATE_PATH} for session {session_id} '
        f'(doc_slug={recovered.doc_slug!r}, {recovered.thread_count} threads'
        f'{", preamble" if recovered.preamble_ts else ""})'
    )
    if no_write_doc:
        return
    # `_scan_sessions` made its own client; make another for the pull.
    # Cheap (no I/O in `__init__`) and keeps the helper contract clean.
    client = _make_slack_client()
    doc = (
        client.pull_doc_staging(state, session_dir=session_dir)
        if staging
        else client.pull_doc_prod(state, session_dir=session_dir)
    )
    text = serialize_doc(doc)
    doc_path = session_dir / f'{recovered.doc_slug}.md'
    doc_path.write_text(text)
    err(f'wrote pulled doc to {doc_path.name}')


# --- Slack CRUD verbs (`slack {history,thread,rm,post,edit,permalink}`) ---


@slack_cli.command("history")
@click.option("-j", "--json", "as_json", is_flag=True, help="Emit raw message dicts as JSON.")
@click.option("-n", "--limit", type=int, default=20, help="Max messages to fetch (default 20).")
@click.argument("channel")
def slack_history(as_json: bool, limit: int, channel: str):
    """List the last N messages in CHANNEL (default 20)."""
    client = _crud_client(channel)
    msgs = client.list_channel_history(client.channel, limit=limit)
    if as_json:
        import json as _json
        click.echo(_json.dumps(msgs, indent=2))
    else:
        rendered = _crud_render_messages(msgs)
        if rendered:
            click.echo(rendered)


@slack_cli.command("thread")
@click.option("-j", "--json", "as_json", is_flag=True, help="Emit raw message dicts as JSON.")
@click.argument("channel")
@click.argument("ts")
def slack_thread(as_json: bool, channel: str, ts: str):
    """Show OP + replies for the thread rooted at TS in CHANNEL."""
    client = _crud_client(channel)
    msgs = client.list_thread_raw(client.channel, ts)
    if as_json:
        import json as _json
        click.echo(_json.dumps(msgs, indent=2))
    else:
        rendered = _crud_render_messages(msgs)
        if rendered:
            click.echo(rendered)


@slack_cli.command("rm")
@click.option("-f", "--force", is_flag=True, help="Delete even if the message has thread replies (`orphans_ok=True`).")
@click.argument("channel")
@click.argument("tss", nargs=-1, required=True, metavar="TS...")
def slack_rm(force: bool, channel: str, tss: tuple[str, ...]):
    """Delete one or more messages from CHANNEL by ts."""
    from .core import OrphanedRepliesError
    client = _crud_client(channel)
    any_failed = False
    for ts in tss:
        try:
            client.delete(ts, orphans_ok=force)
            click.echo(f"{ts}: ok")
        except OrphanedRepliesError as e:
            any_failed = True
            click.echo(f"{ts}: {e} — pass --force to delete anyway", err=True)
        except RuntimeError as e:
            any_failed = True
            click.echo(f"{ts}: {e}", err=True)
    if any_failed:
        raise click.exceptions.Exit(1)


@slack_cli.command("post")
@click.option("-e", "--icon-emoji", help="Per-message `icon_emoji` override (requires `chat:write.customize`).")
@click.option("-i", "--icon-url", help="Per-message `icon_url` override (requires `chat:write.customize`).")
@click.option("-m", "--markdown", "as_markdown", is_flag=True, help="Convert local markdown → Slack mrkdwn (default: send TEXT verbatim).")
@click.option("-t", "--thread-ts", help="Reply to the thread rooted at this ts.")
@click.option("-u", "--username", help="Per-message `username` override (requires `chat:write.customize`).")
@click.argument("channel")
@click.argument("text")
def slack_post(
    icon_emoji: str | None,
    icon_url: str | None,
    as_markdown: bool,
    thread_ts: str | None,
    username: str | None,
    channel: str,
    text: str,
):
    """Post TEXT to CHANNEL; print the new message ts to stdout.

    TEXT is sent verbatim as wire mrkdwn by default (raw). Pass -m to run
    it through the local-md → Slack-mrkdwn conversion first.
    """
    client = _crud_client(channel)
    msg = client.post(
        text,
        thread_id=thread_ts,
        username=username,
        icon_url=icon_url,
        icon_emoji=icon_emoji,
        raw=not as_markdown,
    )
    click.echo(msg.id)


@slack_cli.command("edit")
@click.option("-m", "--markdown", "as_markdown", is_flag=True, help="Convert local markdown → Slack mrkdwn (default: send TEXT verbatim).")
@click.argument("channel")
@click.argument("ts")
@click.argument("text")
def slack_edit(as_markdown: bool, channel: str, ts: str, text: str):
    """Edit the message at TS in CHANNEL to TEXT (raw by default)."""
    client = _crud_client(channel)
    client.edit(ts, text, raw=not as_markdown)


@slack_cli.command("permalink")
@click.argument("channel")
@click.argument("ts")
def slack_permalink(channel: str, ts: str):
    """Print the permalink URL for the message at TS in CHANNEL."""
    client = _crud_client(channel)
    click.echo(client.permalink(ts))


# ============================================================================
# `thrds capture …` subgroup — gist-only trajectory, no platform target.
# ============================================================================


@cli.group("capture")
def capture_cli():
    """Capture-only sessions: git + gist trajectory, no platform posting.

    Same on-disk shape as slack sessions (session dir under
    ``<git-root-or-cwd>/thrds/<slug>/``, git-tracked, gist-mirrored) minus
    any Slack/Discord/etc. plumbing. Useful for drafting posts you'll
    paste manually (Discord in a channel the bot can't reach, arbitrary
    forum, etc.) while still capturing iteration history to a gist.
    """
    pass


@capture_cli.command("init")
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.argument('doc_path')
def capture_init(no_gist: bool, doc_path: str):
    """Initialize a capture-only session for DOC_PATH (no platform target).

    Same auto-resume semantics as `slack init`: if the target dir has a
    partial init (matching platform, gist_id=None), the gist step is
    completed on re-run.
    """
    target, state, was_resume = _do_init(doc_path, no_gist, channel_prefix=None, platform='capture')
    if was_resume:
        return
    hint = None if state.gist_id is None else f"Gist: https://gist.github.com/{state.gist_id}"
    _print_init_completion(target, state, extra_hint=hint)


@capture_cli.command("push")
def capture_push():
    """Commit any local doc changes and push to the gist mirror.

    Capture-only sessions have no platform target — "push" here means
    the gist mirror step alone. No-op if the session was inited with
    `--no-gist` (nothing to push to).
    """
    state = _load_state(expected_platform='capture')
    session_dir = Path.cwd()
    doc_path = _resolve_doc_path(state, None)
    _autocommit(session_dir, [doc_path, str(STATE_PATH)], f'thrds: capture push')
    if state.gist_id is None:
        err("(no gist configured — commit only)")


@capture_cli.command("open")
@click.option('-U', '--no-open', is_flag=True, help='Print the URL, do not launch browser.')
def capture_open(no_open: bool):
    """Open this session's gist in the browser (or print with -U)."""
    state = _load_state(expected_platform='capture')
    if state.gist_id is None:
        raise click.UsageError(
            'No gist recorded — session was init\'d with --no-gist.'
        )
    url = f'https://gist.github.com/{state.gist_id}'
    err(f'Opening gist {state.gist_id}: {url}')
    if not no_open:
        webbrowser.open(url)


# ============================================================================
# `thrds discord …` subgroup — capture + MD-compat lint, no live push.
# ============================================================================
#
# Discord phase 2c (from specs/done/discord-platform.md): the render-preview
# loop without any bot-token / staging-channel plumbing. Prod delivery on
# Discord is copy-paste (self-bots violate ToS); `render` outputs the doc's
# MD to stdout for `| pbcopy`, `lint` flags the constructs that don't render
# in normal Discord user messages (tables, raw @name). Masked links used to
# be flagged too; see specs/done/discord-masked-links-render.md for the fix.


def _run_discord_lint(doc_text: str, doc_path: str) -> int:
    """Shared lint runner used by both `discord lint` and `discord render`.

    Prints the report to stderr; returns the number of issues found so callers
    can decide exit code / whether to still emit output.
    """
    from .lint import DiscordLinter
    report = DiscordLinter().lint(doc_text)
    if report.has_issues:
        err(report.format(path=doc_path))
    return len(report.issues)


@cli.group("discord")
def discord_cli():
    """Discord workflow: init, render (for paste), lint (MD-compat warnings).

    Discord asymmetry: prod delivery is copy-paste (self-bots are ToS-
    prohibited), so there's no `push`. The subgroup captures iteration
    history to a gist (via `init`) and surfaces the final MD (via `render`)
    with warnings about constructs Discord's user-message renderer drops
    on the floor (tables, raw `@name`). See
    `specs/done/discord-platform.md`.
    """
    pass


@discord_cli.command("init")
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.argument('doc_path')
def discord_init(no_gist: bool, doc_path: str):
    """Initialize a `thrds discord` session for DOC_PATH.

    Same on-disk shape as `slack init` / `capture init` — session dir under
    ``<git-root-or-cwd>/thrds/<slug>/``, optionally gist-mirrored. No Slack
    plumbing.
    """
    target, state, was_resume = _do_init(doc_path, no_gist, channel_prefix=None, platform='discord')
    if was_resume:
        return
    hint = None if state.gist_id is None else f"Gist: https://gist.github.com/{state.gist_id}"
    _print_init_completion(target, state, extra_hint=hint)


@discord_cli.command("render")
@click.option('-L', '--no-lint', is_flag=True, help='Skip the MD-compat lint pass (default: run it, warnings → stderr).')
@click.argument('doc_path', required=False)
def discord_render(no_lint: bool, doc_path: str | None):
    """Print DOC_PATH to stdout for paste-into-Discord.

    Idiomatic: ``thrds discord render | pbcopy`` (or set up the `tdc` alias
    in `.thrds-rc`). By default runs the Discord MD-compat lint first and
    prints warnings to stderr; pass ``-L`` to skip.
    """
    state = _load_state(expected_platform='discord')
    path = _resolve_doc_path(state, doc_path)
    text = Path(path).read_text()
    if not no_lint:
        _run_discord_lint(text, path)
    click.echo(text, nl=False)


@discord_cli.command("lint")
@click.argument('doc_path', required=False)
def discord_lint(doc_path: str | None):
    """Run the Discord MD-compat lint on DOC_PATH; print warnings to stderr.

    Exit code is 0 whether or not issues were found (warnings, not errors —
    the doc still renders, just not as intended). Rules:

    \b
    - markdown tables don't render; use a code block or bullets.
    - raw `@name` doesn't ping; needs `<@user_id>`.
    """
    state = _load_state(expected_platform='discord')
    path = _resolve_doc_path(state, doc_path)
    text = Path(path).read_text()
    n = _run_discord_lint(text, path)
    if n == 0:
        err(f"{path}: no issues")


@discord_cli.command("open")
@click.option('-U', '--no-open', is_flag=True, help='Print the URL, do not launch browser.')
def discord_open(no_open: bool):
    """Open this session's gist in the browser (or print with -U)."""
    state = _load_state(expected_platform='discord')
    if state.gist_id is None:
        raise click.UsageError(
            'No gist recorded — session was init\'d with --no-gist.'
        )
    url = f'https://gist.github.com/{state.gist_id}'
    err(f'Opening gist {state.gist_id}: {url}')
    if not no_open:
        webbrowser.open(url)


# ============================================================================
# `thrds bsky …` subgroup — capture + Bluesky-specific lint, no live push.
# ============================================================================
#
# Bluesky's chief drafting pain is the 300-char post limit (per paragraph);
# beyond that, its render dialect is similar to Discord's (bare URLs
# auto-linkify via facets; masked-link syntax renders literal). Same phase-2c
# shape as `discord`: init + render + lint + open, no live push.


def _run_bsky_lint(doc_text: str, doc_path: str) -> int:
    """Shared lint runner for `bsky lint` and `bsky render`.

    Prints the report to stderr; returns the number of issues found.
    """
    from .lint import BskyLinter
    report = BskyLinter().lint(doc_text)
    if report.has_issues:
        err(report.format(path=doc_path))
    return len(report.issues)


@cli.group("bsky")
def bsky_cli():
    """Bluesky workflow: init, render (for paste), lint (post-length + link check).

    Same shape as `discord` — no `push`, since bsky's drafting workflow benefits
    most from the length + link lint alongside a gist-mirrored trajectory.
    The `BskyClient` in `thrds.bsky` (Python API) is unaffected; those wanting
    to script bsky posting directly can still import it.
    """
    pass


@bsky_cli.command("init")
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.argument('doc_path')
def bsky_init(no_gist: bool, doc_path: str):
    """Initialize a `thrds bsky` session for DOC_PATH.

    Same on-disk shape as `slack init` / `discord init` / `capture init` —
    session dir under ``<git-root-or-cwd>/thrds/<slug>/``, optionally
    gist-mirrored. No atproto plumbing.
    """
    target, state, was_resume = _do_init(doc_path, no_gist, channel_prefix=None, platform='bsky')
    if was_resume:
        return
    hint = None if state.gist_id is None else f"Gist: https://gist.github.com/{state.gist_id}"
    _print_init_completion(target, state, extra_hint=hint)


@bsky_cli.command("render")
@click.option('-L', '--no-lint', is_flag=True, help='Skip the MD-compat lint pass (default: run it, warnings → stderr).')
@click.argument('doc_path', required=False)
def bsky_render(no_lint: bool, doc_path: str | None):
    """Print DOC_PATH to stdout for paste-into-Bluesky.

    Idiomatic: ``thrds bsky render | pbcopy`` (or set up the `tbc` alias
    in `.thrds-rc`). By default runs the bsky lint first and prints
    warnings to stderr; pass ``-L`` to skip.
    """
    state = _load_state(expected_platform='bsky')
    path = _resolve_doc_path(state, doc_path)
    text = Path(path).read_text()
    if not no_lint:
        _run_bsky_lint(text, path)
    click.echo(text, nl=False)


@bsky_cli.command("lint")
@click.argument('doc_path', required=False)
def bsky_lint(doc_path: str | None):
    """Run the Bluesky MD-compat lint on DOC_PATH; print warnings to stderr.

    Exit code is 0 whether or not issues were found (warnings, not errors —
    the doc is still postable, just not as intended). Rules:

    \b
    - paragraphs exceeding 300 chars (bsky's post limit).
    - masked links `[text](url)` render as literal text; use bare URLs
      (bsky auto-linkifies via facets).
    """
    state = _load_state(expected_platform='bsky')
    path = _resolve_doc_path(state, doc_path)
    text = Path(path).read_text()
    n = _run_bsky_lint(text, path)
    if n == 0:
        err(f"{path}: no issues")


@bsky_cli.command("open")
@click.option('-U', '--no-open', is_flag=True, help='Print the URL, do not launch browser.')
def bsky_open(no_open: bool):
    """Open this session's gist in the browser (or print with -U)."""
    state = _load_state(expected_platform='bsky')
    if state.gist_id is None:
        raise click.UsageError(
            'No gist recorded — session was init\'d with --no-gist.'
        )
    url = f'https://gist.github.com/{state.gist_id}'
    err(f'Opening gist {state.gist_id}: {url}')
    if not no_open:
        webbrowser.open(url)


def main():
    cli()


def slack_main():
    """Entry point for `slck` — `thrds slack …` without the prefix.

    Invokes the very same :data:`slack_cli` group object, so every verb,
    option and help string is shared; there is nothing to keep in sync. This
    exists instead of per-verb shell aliases because an alias list needs one
    entry per verb and silently goes stale as verbs are added, while a group
    entry point picks up new ones for free.
    """
    slack_cli(prog_name='slck')


if __name__ == '__main__':
    main()
