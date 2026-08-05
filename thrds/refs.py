"""Cross-thread `#slug` refs — the `[text](#slug)` → permalink resolver.

A message body may embed markdown-style links to *other threads within the
same doc*: ``[the profiling thread](#profiling)``. On post the target
thread's permalink doesn't exist yet (chicken/egg: permalink needs ts, ts
needs post). Resolution follows the same two-phase pattern as
`linked.py`:

1. **Placeholder pack** — every ``(#slug)`` replaced with a fixed-length
   placeholder URL and posted. Placeholder length is a safe upper bound
   on real Slack permalinks (~110–140 chars), so the phase-3 rewrite
   never exceeds the phase-2 message length.
2. **Real-URL rewrite** — once every thread is posted, `chat.getPermalink`
   for each referenced slug's OP, then edit each ref-containing message
   in place with the real URL.

The regex only matches ``[text](#slug)`` (slug = alnum/underscore/hyphen);
plain markdown links to external URLs are left alone.
"""
from __future__ import annotations

import re
from typing import Callable

from .doc import Doc, DocMessage, DocThread


# `#slug` uses the same slug charset as the `.md` thread header (`=== slug`),
# so a slug that parses is a slug that can be referenced.
CROSS_REF_RE = re.compile(r'\[([^\]]*)\]\(#([a-zA-Z0-9_-]+)\)')

# Fixed-length placeholder used in phase 2. Length matches `linked.py`'s
# `_detail_url_placeholder`: 180 chars, an upper bound on real Slack
# permalinks (~110–140 for a channel thread reply).
PLACEHOLDER_URL = "x" * 180


def find_refs(content: str) -> list[tuple[str, str]]:
    """Return ``(link_text, slug)`` for each cross-ref in ``content``, in order."""
    return [(m.group(1), m.group(2)) for m in CROSS_REF_RE.finditer(content)]


def substitute_refs(content: str, resolver: Callable[[str], str]) -> str:
    """Rewrite each ``[text](#slug)`` in ``content`` using ``resolver(slug)``.

    ``resolver`` maps slug → URL. The resulting link is
    ``[text](<resolver(slug)>)``. Unmatched brackets or refs pointing outside
    the doc's slug set are the caller's responsibility (see `validate_refs`).
    """
    return CROSS_REF_RE.sub(
        lambda m: f"[{m.group(1)}]({resolver(m.group(2))})",
        content,
    )


def _substitute_message(msg: DocMessage, resolver: Callable[[str], str]) -> DocMessage:
    return DocMessage(content=substitute_refs(msg.content, resolver), author=msg.author)


def _substitute_thread(thread: DocThread, resolver: Callable[[str], str]) -> DocThread:
    return DocThread(
        slug=thread.slug,
        messages=[_substitute_message(m, resolver) for m in thread.messages],
        parent_ts=thread.parent_ts,
    )


def substitute_doc_refs(doc: Doc, resolver: Callable[[str], str]) -> Doc:
    """Return a `Doc` with every message's refs substituted via ``resolver``.

    Preserves `Doc` shape: preamble + threads, per-thread slug + parent_ts,
    per-message author. Pure — the input `Doc` is not mutated.
    """
    return Doc(
        preamble=substitute_refs(doc.preamble, resolver) if doc.preamble else None,
        threads=[_substitute_thread(t, resolver) for t in doc.threads],
    )


def validate_refs(doc: Doc) -> None:
    """Raise `ValueError` if any cross-ref points to a slug not in the doc.

    Cross-refs to non-existent slugs would silently render as broken links
    once the placeholder gets replaced. Fail early with an actionable message
    listing every dangling ref (thread + message + slug).
    """
    doc_slugs = {t.slug for t in doc.threads if t.slug is not None}
    dangling: list[str] = []
    for thread in doc.threads:
        loc = thread.slug or "<unnamed>"
        for i, msg in enumerate(thread.messages):
            for _text, slug in find_refs(msg.content):
                if slug not in doc_slugs:
                    dangling.append(f"thread {loc!r}, msg {i}: #{slug}")
    if doc.preamble is not None:
        for _text, slug in find_refs(doc.preamble):
            if slug not in doc_slugs:
                dangling.append(f"preamble: #{slug}")
    if dangling:
        raise ValueError(
            "Cross-refs to unknown slugs (add threads or fix the refs):\n  "
            + "\n  ".join(dangling)
        )


def doc_has_refs(doc: Doc) -> bool:
    """True iff any message (preamble or thread) contains a `[text](#slug)` ref."""
    if doc.preamble is not None and CROSS_REF_RE.search(doc.preamble):
        return True
    for thread in doc.threads:
        for msg in thread.messages:
            if CROSS_REF_RE.search(msg.content):
                return True
    return False


def thread_has_refs(thread: DocThread) -> bool:
    """True iff any of ``thread``'s messages contains a `[text](#slug)` ref."""
    return any(CROSS_REF_RE.search(m.content) for m in thread.messages)
