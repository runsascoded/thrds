"""Rewrite a legacy session's *git history* into the per-thread layout.

``thrds slack migrate`` converts a session's working tree. This module converts
its history: every commit is rebuilt with the doc split into ``NN-slug.md``
files, preserving each commit's message, author, committer, and both
timestamps. The result is a new branch; nothing is force-pushed here.

Why rewrite rather than convert-going-forward
---------------------------------------------
These sessions are collected as writing examples, and the gist history *is* the
artifact. A reader should not have to learn a retired ``===``-multi-thread-
per-file syntax to follow how a message evolved. This is a *format* migration:
every version's content is preserved exactly; only its distribution across
files changes.

Why not ``rebase --root``
-------------------------
Rebase replays *diffs*, and the tree shape changes completely at the first
commit — every later patch would conflict. Each commit's tree is instead built
directly from that commit's doc, so commits are independent and nothing can
conflict.

Stable numbering is the whole game
----------------------------------
Indices must be assigned **globally**, not per commit. Trainium's history
inserts ``tflops-q`` at position 2 in its third commit; numbering each commit
independently would renumber ``profiling`` 02→03, ``nki`` 03→04 and
``segfault`` 04→05 — three renames, and a rename breaks the per-file history
that is the entire point of the layout. So a slug gets one index for all time,
taken from the final commit's ordering, and a commit where a thread doesn't
exist yet simply has a gap. :func:`assign_indices` owns this.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .doc import Doc, DocMessage, DocThread
from .md import parse_doc, parse_thread, serialize_thread
from .migrate import PREAMBLE_INDEX, PREAMBLE_SLUG
from .state import SessionState, ThreadEntry
from .threadfile import thread_filename


class ReplayError(RuntimeError):
    """Replay could not proceed (bad ref, missing doc, verification failure)."""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    """Run a git command in ``repo``, returning stdout; raise `ReplayError` on failure."""
    try:
        r = subprocess.run(
            ['git', *args], cwd=repo, check=True, capture_output=True, text=True,
            input=stdin, env=env,
        )
    except subprocess.CalledProcessError as e:
        raise ReplayError(
            f"git {' '.join(args)} failed (exit {e.returncode}):\n{e.stderr.rstrip()}"
        ) from e
    return r.stdout


@dataclass
class CommitPlan:
    """One rewritten commit: its source sha and the files its tree will hold.

    Files split into two kinds. ``new_files`` are ones this rewrite authors
    (the split thread files, the rebuilt ``thrds.json``) and carry content.
    ``keep`` are untouched files, carried by *blob sha* rather than content —
    which sidesteps decoding binaries like downloaded emoji PNGs, and
    guarantees an unchanged file is bit-identical to the original.
    """
    sha: str
    subject: str
    new_files: dict[str, str] = field(default_factory=dict)          # name → content
    keep: dict[str, tuple[str, str]] = field(default_factory=dict)   # name → (mode, blob sha)
    slugs: list[str] = field(default_factory=list)

    @property
    def md_names(self) -> list[str]:
        return sorted(n for n in self.new_files if n.endswith('.md'))


@dataclass
class ReplayResult:
    """Outcome of a replay: the new branch tip and per-commit detail."""
    branch: str
    head: str
    commits: list[CommitPlan] = field(default_factory=list)
    index_by_slug: dict[str, int] = field(default_factory=dict)


def commits_in(repo: Path, ref: str) -> list[str]:
    """Every commit reachable from ``ref``, oldest first."""
    out = _git(repo, 'rev-list', '--reverse', ref).strip()
    return out.split('\n') if out else []


def _blob(repo: Path, sha: str, path: str) -> str | None:
    """File contents at ``sha``, or None when the path doesn't exist there."""
    r = subprocess.run(
        ['git', 'show', f'{sha}:{path}'], cwd=repo, capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else None


def _tree_entries(repo: Path, sha: str) -> dict[str, tuple[str, str]]:
    """``{name: (mode, blob_sha)}`` for every path in ``sha``'s tree.

    Session dirs are flat, so ``-r`` yields plain filenames and the result can
    be handed straight to ``mktree``.
    """
    out = _git(repo, 'ls-tree', '-r', sha).strip()
    entries: dict[str, tuple[str, str]] = {}
    for line in out.split('\n') if out else []:
        meta, _, name = line.partition('\t')
        mode, _kind, blob = meta.split()
        entries[name] = (mode, blob)
    return entries


def assign_indices(slug_orders: list[list[str]]) -> dict[str, int]:
    """Map each slug to one stable index, for all commits.

    ``slug_orders`` is each commit's thread order, oldest first. The **last**
    commit's ordering is canonical — it's the end state a reader sees, and
    keeping it authoritative means the final tree is numbered exactly as a
    fresh `migrate` would number it. Slugs that appear earlier but not at the
    end (a thread that was cut) are appended afterward, most-recently-seen
    first, so they never displace a surviving thread's number.

    The preamble is always index 0, reserved whether or not one is present.
    """
    canonical = slug_orders[-1] if slug_orders else []
    index: dict[str, int] = {}
    n = 1
    for slug in canonical:
        if slug not in index:
            index[slug] = n
            n += 1
    for order in reversed(slug_orders[:-1]):
        for slug in order:
            if slug not in index:
                index[slug] = n
                n += 1
    index[PREAMBLE_SLUG] = PREAMBLE_INDEX
    return index


def _thread_files_for(doc: Doc, index_by_slug: dict[str, int]) -> dict[str, str]:
    """Split ``doc`` into ``{filename: content}`` using the global index map."""
    files: dict[str, str] = {}
    if doc.preamble:
        preamble = DocThread(
            messages=[DocMessage(content=doc.preamble)], slug=PREAMBLE_SLUG,
        )
        files[thread_filename(PREAMBLE_INDEX, PREAMBLE_SLUG)] = serialize_thread(preamble)
    for thread in doc.threads:
        if thread.slug is None:
            raise ReplayError(
                'Cannot replay a commit whose doc has an unslugged `===` thread; '
                'a slug is the thread\'s filename.'
            )
        files[thread_filename(index_by_slug[thread.slug], thread.slug)] = serialize_thread(thread)
    return files


def _rewrite_state(text: str, doc: Doc, doc_path: str) -> str:
    """Rebuild a commit's ``thrds.json`` in the per-thread shape.

    Rewritten rather than carried through so that checking out *any* commit of
    the replayed history yields a session the current code can actually
    operate on — a history that is per-thread in its files but legacy in its
    state would be internally inconsistent at every point.
    """
    import json

    data = json.loads(text)
    staging = data.get('staging_threads') or {}
    prod = data.get('prod_threads') or {}
    slugs = [t.slug for t in doc.threads] + ([PREAMBLE_SLUG] if doc.preamble else [])

    threads: dict[str, ThreadEntry] = {}
    for slug in slugs:
        placements = [(c, by[slug]) for c, by in sorted(prod.items()) if slug in by]
        staging_ts = (
            data.get('staging_preamble_ts') if slug == PREAMBLE_SLUG else staging.get(slug)
        )
        if placements:
            channel, posted_ts = placements[0]
            threads[slug] = ThreadEntry(
                staging_ts=staging_ts,
                target={'channel': channel},
                state='posted',
                posted_ts=posted_ts,
            )
        else:
            threads[slug] = ThreadEntry(staging_ts=staging_ts)

    state = SessionState(**{
        **{k: v for k, v in data.items() if k in SessionState.__dataclass_fields__},
        'doc_path': None,
        'session_slug': data.get('session_slug') or Path(doc_path).stem,
        'staging_threads': {},
        'prod_threads': {},
        'prod_preamble_ts': {},
        'staging_preamble_ts': None,
        'threads': threads,
    })
    from dataclasses import asdict
    return json.dumps(asdict(state), indent=2) + '\n'


def plan_replay(repo: Path, ref: str, doc_path: str) -> tuple[list[CommitPlan], dict[str, int]]:
    """Compute every rewritten commit's tree. Touches no refs.

    Commits whose tree has no doc at ``doc_path`` are carried through with
    their files unchanged — a session's history may start before the doc
    existed.
    """
    shas = commits_in(repo, ref)
    if not shas:
        raise ReplayError(f'No commits reachable from {ref!r}.')

    docs: dict[str, Doc] = {}
    orders: list[list[str]] = []
    for sha in shas:
        text = _blob(repo, sha, doc_path)
        if text is None:
            continue
        doc = parse_doc(text).doc
        docs[sha] = doc
        orders.append([t.slug for t in doc.threads if t.slug is not None])

    if not docs:
        raise ReplayError(f'No commit reachable from {ref!r} contains {doc_path!r}.')

    index_by_slug = assign_indices(orders)

    plans: list[CommitPlan] = []
    for sha in shas:
        subject = _git(repo, 'log', '-1', '--format=%s', sha).strip()
        keep = {n: e for n, e in _tree_entries(repo, sha).items() if n != doc_path}
        new_files: dict[str, str] = {}
        doc = docs.get(sha)
        if doc is not None:
            new_files.update(_thread_files_for(doc, index_by_slug))
            if 'thrds.json' in keep:
                old_state = _blob(repo, sha, 'thrds.json')
                if old_state is not None:
                    new_files['thrds.json'] = _rewrite_state(old_state, doc, doc_path)
                    del keep['thrds.json']
        plans.append(CommitPlan(
            sha=sha,
            subject=subject,
            new_files=new_files,
            keep=keep,
            slugs=[t.slug for t in doc.threads] if doc is not None else [],
        ))
    return plans, index_by_slug


def verify_plan(repo: Path, plans: list[CommitPlan], doc_path: str) -> list[str]:
    """Check each rewritten commit round-trips to the same threads. Returns problems.

    This is the property that makes the rewrite safe to force-push: for every
    commit, parsing the new per-thread files must reproduce exactly the threads
    (and preamble) that parsing the old doc produced. An empty list means the
    new format lost nothing anywhere in the history.
    """
    problems: list[str] = []
    for plan in plans:
        old_text = _blob(repo, plan.sha, doc_path)
        if old_text is None:
            continue
        old = parse_doc(old_text).doc
        rebuilt: list[DocThread] = []
        preamble_seen: str | None = None
        for name in plan.md_names:
            slug = name[3:-3]
            thread = parse_thread(plan.new_files[name], slug=slug).thread
            if slug == PREAMBLE_SLUG:
                preamble_seen = thread.messages[0].content
            else:
                rebuilt.append(thread)
        by_index = {t.slug: t for t in rebuilt}
        expected = [t for t in old.threads]
        got = [by_index.get(t.slug) for t in expected]
        if got != expected:
            for e, g in zip(expected, got):
                if e != g:
                    problems.append(f'{plan.sha[:8]}: thread {e.slug!r} differs after split')
        old_pre = old.preamble.strip() if old.preamble else None
        if (preamble_seen or None) != old_pre:
            problems.append(f'{plan.sha[:8]}: preamble differs after split')
    return problems


def write_replay(repo: Path, plans: list[CommitPlan], branch: str) -> str:
    """Build the rewritten commits and point ``branch`` at the result.

    Uses plumbing (``hash-object`` / ``mktree`` / ``commit-tree``) rather than
    rebase, so each commit's tree is authored directly and no patch is ever
    applied. Author, committer, and both dates are copied from the original.
    """
    parent: str | None = None
    for plan in plans:
        entries: dict[str, tuple[str, str]] = dict(plan.keep)
        for name, content in plan.new_files.items():
            blob = _git(repo, 'hash-object', '-w', '--stdin', stdin=content).strip()
            entries[name] = ('100644', blob)
        lines = [f'{mode} blob {blob}\t{name}' for name, (mode, blob) in sorted(entries.items())]
        tree = _git(repo, 'mktree', stdin='\n'.join(lines) + '\n').strip()

        fmt = '%an%n%ae%n%aI%n%cn%n%ce%n%cI'
        an, ae, ad, cn, ce, cd = _git(repo, 'log', '-1', f'--format={fmt}', plan.sha).rstrip('\n').split('\n')
        message = _git(repo, 'log', '-1', '--format=%B', plan.sha)

        import os
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': an, 'GIT_AUTHOR_EMAIL': ae, 'GIT_AUTHOR_DATE': ad,
            'GIT_COMMITTER_NAME': cn, 'GIT_COMMITTER_EMAIL': ce, 'GIT_COMMITTER_DATE': cd,
        }
        args = ['commit-tree', tree]
        if parent is not None:
            args += ['-p', parent]
        parent = _git(repo, *args, env=env, stdin=message).strip()

    if parent is None:
        raise ReplayError('Nothing to replay.')
    _git(repo, 'branch', '-f', branch, parent)
    return parent


def replay(repo: Path | str, doc_path: str, ref: str = 'HEAD', branch: str = 'per-thread') -> ReplayResult:
    """Plan, verify, and write a per-thread rewrite of ``ref`` onto ``branch``."""
    r = Path(repo)
    plans, index_by_slug = plan_replay(r, ref, doc_path)
    problems = verify_plan(r, plans, doc_path)
    if problems:
        raise ReplayError(
            'Replay verification failed — the rewrite would lose content:\n  '
            + '\n  '.join(problems)
        )
    head = write_replay(r, plans, branch)
    return ReplayResult(branch=branch, head=head, commits=plans, index_by_slug=index_by_slug)
