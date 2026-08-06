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
import webbrowser
from pathlib import Path

import click

from . import mirror
from .doc import Doc
from .md import diff_docs, parse_doc, serialize_doc
from .slack import SlackClient
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


@cli.command()
@click.option('-G', '--no-gist', is_flag=True, help='Skip gist creation; local git repo only.')
@click.option('-p', '--prefix', help='Staging PC channel-name prefix override (else `THRDS_CHANNEL_PREFIX` env, else empty).')
@click.argument('doc_path')
def init(no_gist: bool, prefix: str | None, doc_path: str):
    """Initialize a `thrds` session for DOC_PATH (a `.md` file).

    Creates ``<git-root-or-cwd>/thrds/<slug>/`` (ghpr-style), copies or
    creates the doc there, `git init`s the session dir, and by default
    creates a secret gist and adds it as the ``g`` remote. Refuses if the
    target dir already exists.
    """
    doc_p = Path(doc_path)
    slug = doc_p.stem
    if not slug:
        raise click.UsageError(f"cannot derive a slug from DOC_PATH: {doc_path!r}")
    target = mirror.resolve_session_dir(Path.cwd(), slug)
    if target.exists():
        raise click.UsageError(f"Target session dir already exists: {target}")
    target.mkdir(parents=True)

    dest_md = target / f'{slug}.md'
    if doc_p.exists() and doc_p.is_file():
        dest_md.write_text(doc_p.read_text())
    else:
        # Empty placeholder — user edits before first push.
        dest_md.write_text('')

    state = SessionState.new(doc_path=f'{slug}.md', channel_prefix=prefix)
    mirror.init_repo(target)

    if no_gist:
        # Local-only path: commit doc + state.json under our own initial commit.
        state.save(target)
        mirror.commit(target, [f'{slug}.md', str(STATE_PATH)], f'thrds: init {slug}')
    else:
        # Gist-mirrored path: create gist FIRST (it seeds an initial commit
        # from the doc), align local HEAD to that commit, then add state.json
        # as our fast-forward commit on top. This way local and gist share the
        # same history from commit 1.
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
    doc = client.pull_doc_prod(state, channel=channel) if prod else client.pull_doc_staging(state)
    text = serialize_doc(doc)
    if write:
        path = _resolve_doc_path(state, doc_path)
        Path(path).write_text(text)
        err(f"wrote pulled doc to {path}")
        mode = 'prod' if prod else 'staging'
        _autocommit(Path.cwd(), [path], f'thrds: pull {mode} → {path}')
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
    slack_doc = client.pull_doc_prod(state, channel=channel) if prod else client.pull_doc_staging(state)
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
