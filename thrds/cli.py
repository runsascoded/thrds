"""Command-line interface for `thrds` sessions.

Wraps the `Doc` / `SessionState` / `SlackClient` primitives into
init/push/pull/diff/archive subcommands. Session state lives at
``.thrds/state.json`` in the current working directory (see
`thrds.state`); the CLI is a thin operational layer over the library.

Slack access requires ``SLACK_THRDS_USER_TOKEN`` (a user-scoped ``xoxp-``
token with ``chat:write`` + ``groups:write`` — see the spec for why user
scope is load-bearing). Read commands (``pull``, ``diff``) also require
``users:read`` for foreign-author resolution.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from .doc import Doc
from .md import diff_docs, parse_doc, serialize_doc
from .slack import SlackClient
from .state import SessionState


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
    """Load ``.thrds/state.json`` from CWD or exit clearly."""
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


@click.group()
def cli():
    """`thrds`: draft multi-thread posts locally, sync to Slack (staging + prod)."""
    pass


@cli.command()
@click.option('-p', '--prefix', help='Staging PC channel-name prefix override (else `THRDS_CHANNEL_PREFIX` env, else empty).')
@click.argument('doc_path')
def init(prefix: str | None, doc_path: str):
    """Initialize a `thrds` session for DOC_PATH (a `.md` file).

    Creates `.thrds/state.json` with a fresh session_id. Refuses if
    state.json already exists — rm to start fresh.
    """
    if Path('.thrds/state.json').exists():
        raise click.UsageError('.thrds/state.json already exists — rm to start fresh.')
    state = SessionState.new(doc_path=doc_path, channel_prefix=prefix)
    state.save()
    err(f"Initialized session {state.session_id} for {doc_path}")
    err(f"Staging PC name (on first push): {state.staging_channel_name()}")


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
        _print_sync_summary('pushed to prod' + (' (dry-run)' if dry_run else ''), result)
        if not dry_run and not keep_staging and state.staging_channel is not None:
            err(f"  archived staging PC: {state.staging_channel}")
    else:
        result = client.sync_doc_staging(doc, state, dry_run=dry_run)
        _print_sync_summary('pushed to staging' + (' (dry-run)' if dry_run else ''), result)


@cli.command()
@click.option('-c', '--channel', help='Prod channel override (only with --prod).')
@click.option('-p', '--prod', is_flag=True, help='Pull from prod channel.')
@click.option('-w', '--write', is_flag=True, help='Write the pulled doc back to DOC_PATH.')
@click.argument('doc_path', required=False)
def pull(channel: str | None, prod: bool, write: bool, doc_path: str | None):
    """Pull the doc's current state from Slack. Default: staging PC.

    Without ``--write`` the doc is printed to stdout (frontmatter omitted).
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

    A no-op if no staging PC exists (nothing was pushed to staging yet, or
    it was already archived on prod push).
    """
    state = _load_state()
    if state.staging_channel is None:
        err("No staging PC to archive.")
        return
    client = _make_slack_client()
    client.archive_channel(state.staging_channel)
    err(f"archived staging PC: {state.staging_channel}")


def main():
    cli()


if __name__ == '__main__':
    main()
