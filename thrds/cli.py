"""Command-line interface for `thrds` sessions.

Wraps the `Doc` / `SessionState` / `SlackClient` primitives into
init/push/pull/diff/archive/open subcommands. Each session lives in its own
directory (ghpr-style: ``<git-root-or-cwd>/thrds/<slug>/``) with its
own private git repo (nested `.git/` is invisible to any surrounding
project's git). A secret gist created at init becomes the ``g`` remote;
state-mutating verbs (`push`, `pull --write`, `archive`) auto-commit +
push, so the mirror always reflects the current session state.

Slack access requires ``SLACK_THRDS_USER_TOKEN`` (a user-scoped ``xoxp-``
token with ``chat:write`` + ``groups:write`` — see the spec for why user
scope is load-bearing). Read commands (``pull``, ``diff``) also require
``users:read`` for foreign-author resolution.
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


SLACK_TOKEN_ENV = 'SLACK_THRDS_USER_TOKEN'


def _make_slack_client() -> SlackClient:
    """Instantiate a `SlackClient` from ``SLACK_THRDS_USER_TOKEN``.

    ``channel`` is initialized to ``""`` — every doc-level method
    (``sync_doc_*``, ``pull_doc_*``) swaps ``self.channel`` internally per
    operation, so the init value doesn't matter.
    """
    token = os.environ.get(SLACK_TOKEN_ENV)
    if not token:
        raise click.UsageError(
            f"{SLACK_TOKEN_ENV} not set — add a `xoxp-` user token to your env."
        )
    return SlackClient(token=token, channel="")


def _load_state() -> SessionState:
    """Load ``thrds.json`` from CWD or exit clearly."""
    try:
        return SessionState.load()
    except FileNotFoundError as e:
        raise click.UsageError(str(e))


def _resolve_doc_path(state: SessionState, arg: str | None) -> str:
    """Resolve the doc path: explicit arg wins, else state.doc_path, else error."""
    path = arg if arg is not None else state.doc_path
    if path is None:
        raise click.UsageError(
            "No doc path — pass DOC_PATH or run `thrds init <doc.md>` first."
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
    removed `.git/`). No-op if there's no `g` remote (``thrds init --no-gist``).
    """
    if not mirror.is_git_repo(session_root):
        return
    try:
        mirror.commit_and_push(session_root, paths, message)
    except mirror.MirrorError as e:
        err(f"warning: mirror commit/push failed: {e}")


@click.group()
def cli():
    """`thrds`: draft multi-thread posts locally, sync to Slack (staging + prod)."""
    pass


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
            "Re-run `thrds init` with `--no-gist` to skip the gist mirror."
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


@cli.command()
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.option('-p', '--prefix', help='Staging PC channel-name prefix override (else `THRDS_CHANNEL_PREFIX` env, else empty).')
@click.argument('doc_path')
def init(no_gist: bool, prefix: str | None, doc_path: str):
    """Initialize a `thrds` session for DOC_PATH (a `.md` file).

    Creates ``<git-root-or-cwd>/thrds/<slug>/`` (ghpr-style), copies or
    creates the doc there, `git init`s the session dir, and by default
    creates a secret gist and adds it as the ``g`` remote.

    Auto-resume: if the target dir already exists and holds a valid
    ``thrds.json`` with ``gist_id: null``, finishes the gist-creation
    step instead of refusing (recovers from an earlier init that failed
    at the gist step — e.g. network hiccup, `gh` unauth'd).
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
        if existing.gist_id is not None:
            raise click.UsageError(
                f"Target session dir already fully initialized (gist_id={existing.gist_id}): {target}\n"
                f"Use `thrds open` to browse, or delete the dir and re-run to reset."
            )
        if no_gist:
            # gist_id=None + --no-gist = re-running the same intent; nothing to do.
            raise click.UsageError(
                f"Target session dir already exists (no-gist mode): {target}"
            )
        err(f"Resuming partial init at {target} (gist step)...")
        gist_id = _do_gist_init(target, existing, slug)
        err(f"Gist created: https://gist.github.com/{gist_id}")
        return

    target.mkdir(parents=True)

    dest_md = target / f'{slug}.md'
    if doc_p.exists() and doc_p.is_file():
        dest_md.write_text(doc_p.read_text())
    else:
        # Empty placeholder — user edits before first push.
        dest_md.write_text('')

    state = SessionState.new(doc_path=f'{slug}.md', channel_prefix=prefix)
    mirror.init_repo(target)
    # Save state.json BEFORE anything that can fail (gist creation, network,
    # `gh` auth). If a subsequent step blows up, the target dir has a
    # detectable "partial init" marker (state.json with gist_id=None) that
    # a re-run of `thrds init` picks up as resumable.
    state.save(target)

    if no_gist:
        mirror.commit(target, [f'{slug}.md', str(STATE_PATH)], f'thrds: init {slug}')
    else:
        # Gist-mirrored path: create gist FIRST (it seeds an initial commit
        # from the doc), align local HEAD to that commit, then re-save
        # state.json (now with gist_id) as our fast-forward commit on top.
        # This way local and gist share the same history from commit 1.
        _do_gist_init(target, state, slug)

    err(f"Initialized session {state.session_id} at {target}")
    err(f"Staging PC name (on first push): {state.staging_channel_name()}")
    try:
        rel = target.relative_to(Path.cwd())
        err(f"cd {rel} to work on this doc")
    except ValueError:
        # target isn't under cwd (git-root was above cwd). Print absolute.
        err(f"cd {target} to work on this doc")


@cli.command()
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-k', '--keep-staging', is_flag=True, help='Do not auto-archive the staging PC after --prod.')
@click.option('-n', '--dry-run', is_flag=True, help='Show plan without side effects.')
@click.option('-p', '--prod', is_flag=True, help='Push to prod (additive) instead of staging (terraform).')
@click.argument('doc_path', required=False)
def push(channel: str | None, keep_staging: bool, dry_run: bool, prod: bool, doc_path: str | None):
    """Push DOC_PATH to Slack. Default: staging PC (terraform)."""
    state = _load_state()
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


@cli.command()
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-p', '--prod', is_flag=True, help='Pull from prod channel.')
@click.option('-w', '--write', is_flag=True, help='Write the pulled doc back to DOC_PATH.')
@click.argument('doc_path', required=False)
def pull(channel: str | None, prod: bool, write: bool, doc_path: str | None):
    """Pull the doc's current state from Slack. Default: staging PC.

    Without ``--write`` the doc is printed to stdout (frontmatter omitted).
    With ``--write``, DOC_PATH is overwritten and (if the session is a git
    repo) auto-committed + pushed to the gist mirror.
    """
    state = _load_state()
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


@cli.command()
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-p', '--prod', is_flag=True, help='Diff against prod channel.')
@click.argument('doc_path', required=False)
def diff(channel: str | None, prod: bool, doc_path: str | None):
    """Diff local DOC_PATH against Slack's current state. Default: staging PC."""
    state = _load_state()
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


@cli.command()
def archive():
    """Archive this session's staging PC (reversible via Slack UI/API `unarchive`).

    Idempotent — subsequent invocations no-op via the ``state.staging_archived``
    flag. Auto-commits + pushes the flag change to the gist mirror.
    """
    state = _load_state()
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


@cli.command('open')
@click.option('-p', '--prod', is_flag=True, help='Open the prod channel (default: gist).')
@click.option('-s', '--staging', is_flag=True, help='Open the staging PC (default: gist).')
@click.option('-U', '--no-open', is_flag=True, help='Print the URL, do not launch browser.')
def open_(prod: bool, staging: bool, no_open: bool):
    """Open this session's gist (default), staging PC, or prod channel in the browser.

    Mirrors ``ghpr open [-g]`` — with three targets instead of two, since a
    thrds session tracks a gist + a staging channel + (once pushed) a prod
    channel. Pass ``-U`` to just print the URL (useful in scripts).
    """
    if prod and staging:
        raise click.UsageError('--prod and --staging are mutually exclusive.')
    state = _load_state()

    if staging:
        if state.staging_channel is None:
            raise click.UsageError(
                'No staging channel — run `thrds push` first to create one.'
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


@cli.command('list-sessions')
@click.option('-c', '--cursor', help='Slack pagination cursor to resume from.')
@click.option('-d', '--oldest-days', type=float, help='Only scan messages posted in the last N days.')
@click.option('-D', '--latest-days', type=float, help='Skip messages newer than N days ago.')
@click.option('-m', '--max-pages', type=int, default=50, show_default=True, help='Cap on `conversations.history` pages fetched. Pass 0 to disable.')
@click.argument('channel')
def list_sessions(cursor, oldest_days, latest_days, max_pages, channel):
    """List thrds sessions found in CHANNEL by scanning message metadata.

    Same scan machinery as `thrds recover` (same window / cap / cursor
    flags), but read-only — never writes state or pulls the doc. Useful
    to figure out which session_id you actually want before running
    `recover -i <sid>`, or just to see what's in a channel.
    """
    oldest_ts, latest_ts, cur, cap = _scan_bounds(cursor, oldest_days, latest_days, max_pages)
    sessions, channel = _scan_sessions(channel, oldest_ts, latest_ts, cur, cap)
    if not sessions:
        err(f'No thrds-metadata sessions found in {channel}.')
        return
    err(f'{len(sessions)} session{"s" if len(sessions) != 1 else ""} in {channel}:')
    _print_sessions_table(sessions, channel)


@cli.command()
@click.option('-c', '--cursor', help='Slack pagination cursor to resume from (printed by a prior `ScanCapReached`).')
@click.option('-d', '--oldest-days', type=float, help='Only scan messages posted in the last N days (default: unbounded).')
@click.option('-D', '--latest-days', type=float, help='Skip messages newer than N days ago (Slack `latest` upper bound). Pair with `--oldest-days` to scan a window.')
@click.option('-i', '--session-id', help='Session ID to recover (required if channel holds >1 session).')
@click.option('-m', '--max-pages', type=int, default=50, show_default=True, help='Safety cap on `conversations.history` pages fetched (200 msgs/page). Pass 0 to disable.')
@click.option('-s', '--staging', is_flag=True, help='Route recovered pointers to staging_threads (default: prod_threads[channel]).')
@click.option('-W', '--no-write-doc', is_flag=True, help='Skip the doc-pull step; write thrds.json only.')
@click.argument('channel')
def recover(
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
            'cd into a fresh session dir before running `thrds recover`.'
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
    # (not freshly minted) — that's the whole point of recovery.
    state = SessionState(
        session_id=recovered.session_id,
        doc_path=f'{recovered.doc_slug}.md',
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


def main():
    cli()


if __name__ == '__main__':
    main()
