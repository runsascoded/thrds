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
from .md import diff_docs, parse_doc, serialize_doc
from .slack import ScanCapReached, SlackClient
from .state import STATE_PATH, SessionState


def err(*msg):
    """stderr `print`. A plain function (not `partial(print, file=sys.stderr)`)
    so `sys.stderr` is resolved at call time — click's CliRunner patches it
    per-invocation, and a partial baked-in reference would bypass that."""
    click.echo(" ".join(str(m) for m in msg), err=True)


SLACK_TOKEN_ENV = 'THRDS_SLACK_TOKEN'
SLACK_TOKEN_ENV_DEPRECATED = 'SLACK_THRDS_USER_TOKEN'

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
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-k', '--keep-staging', is_flag=True, help='Do not auto-archive the staging PC after --prod.')
@click.option('-n', '--dry-run', is_flag=True, help='Show plan without side effects.')
@click.option('-p', '--prod', is_flag=True, help='Push to prod (additive) instead of staging (terraform).')
@click.argument('doc_path', required=False)
def slack_push(channel: str | None, keep_staging: bool, dry_run: bool, prod: bool, doc_path: str | None):
    """Push DOC_PATH to Slack. Default: staging PC (terraform)."""
    state = _load_state(expected_platform='slack')
    doc = _load_doc(state, doc_path)
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    if not prod and keep_staging:
        raise click.UsageError('--keep-staging requires --prod.')
    client = _make_slack_client()
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
@click.option('-p', '--prod', is_flag=True, help='Pull from prod channel.')
@click.option('-w', '--write', is_flag=True, help='Write the pulled doc back to DOC_PATH.')
@click.argument('doc_path', required=False)
def slack_pull(channel: str | None, prod: bool, write: bool, doc_path: str | None):
    """Pull the doc's current state from Slack. Default: staging PC.

    Without ``--write`` the doc is printed to stdout (frontmatter omitted).
    With ``--write``, DOC_PATH is overwritten and (if the session is a git
    repo) auto-committed + pushed to the gist mirror.
    """
    state = _load_state(expected_platform='slack')
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    client = _make_slack_client()
    if channel is not None:
        channel = _resolve_channel(client, channel)
    session_dir = Path.cwd()
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


@slack_cli.command("diff")
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-p', '--prod', is_flag=True, help='Diff against prod channel.')
@click.argument('doc_path', required=False)
def slack_diff(channel: str | None, prod: bool, doc_path: str | None):
    """Diff local DOC_PATH against Slack's current state. Default: staging PC."""
    state = _load_state(expected_platform='slack')
    if not prod and channel is not None:
        raise click.UsageError('--channel requires --prod.')
    local_doc = _load_doc(state, doc_path)
    client = _make_slack_client()
    if channel is not None:
        channel = _resolve_channel(client, channel)
    session_dir = Path.cwd()
    slack_doc = (
        client.pull_doc_prod(state, channel=channel, session_dir=session_dir)
        if prod
        else client.pull_doc_staging(state, session_dir=session_dir)
    )
    click.echo(diff_docs(local_doc, slack_doc, from_label='local', to_label='slack'), nl=False)


@slack_cli.command("archive")
def slack_archive():
    """Archive this session's staging PC (reversible via Slack UI/API `unarchive`).

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
    client = _make_slack_client()
    client.archive_channel(state.staging_channel)
    state.staging_archived = True
    state.save()
    err(f"archived staging PC: {state.staging_channel}")
    _autocommit(Path.cwd(), [str(STATE_PATH)], f'thrds: archive {state.staging_channel}')


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
# in normal Discord user messages (masked links, tables, raw @name).


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
    on the floor (masked links, tables, raw `@name`). See
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
    - masked links `[text](url)` render as literal text in normal Discord
      messages (bot-embed messages are the exception); use bare URLs.
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


if __name__ == '__main__':
    main()
