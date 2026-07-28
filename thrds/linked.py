"""Linked summary threads: summary messages with links to detail messages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MessageAtoms:
    """Which content pieces (prefix, bullet indices, suffix) landed in a message.

    Emitted by ``build_summary_partition`` for phase-1 packing so that phase-4
    can rebuild each message identically using real (rather than placeholder)
    URLs. Preserving the partition guarantees phase-4 message count == phase-1
    count without depending on the greedy packer producing identical output
    across two URL-length regimes.
    """
    bullet_indices: list[int]
    has_prefix: bool = False
    has_suffix: bool = False


@dataclass
class Section:
    """A section with a summary bullet and detail body."""
    title: str
    summary: str
    body: str


@dataclass
class LinkedThread:
    """A thread with summary messages linking to detail messages."""
    summary_prefix: str
    sections: list[Section]
    summary_suffix: str = ""


@dataclass
class LinkedSyncResult:
    """Result of syncing a linked thread."""
    thread_id: str
    summary_ids: list[str]
    detail_ids: list[str]
    section_detail_ids: dict[str, str]  # section title → first detail message ID


_CONT_PREFIX = "… "
_CONT_SUFFIX = " …"


def _hard_split(text: str, limit: int) -> list[str]:
    """Split `text` into chunks ≤ `limit`, with ellipsis continuation markers.

    Prefers word-boundary splits; falls back to a hard character split when
    no space sits far enough into the chunk to leave meaningful content.
    Every returned chunk has ``len(chunk) <= limit`` by construction.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        prefix = "" if not parts else _CONT_PREFIX
        room = limit - len(prefix) - len(_CONT_SUFFIX)
        # Word boundary is only useful if it leaves > half the chunk used,
        # else the chunk is mostly wasted (e.g. bullet marker with no
        # summary content). Fall back to hard char split otherwise.
        cut = remaining.rfind(" ", room * 3 // 4, room + 1)
        if cut <= 0:
            cut = room
        parts.append(f"{prefix}{remaining[:cut].rstrip()}{_CONT_SUFFIX}")
        remaining = remaining[cut:].lstrip()
    if remaining:
        prefix = "" if not parts else _CONT_PREFIX
        parts.append(f"{prefix}{remaining}")
    return parts


def split_body(body: str, limit: int) -> list[str]:
    """Split a body into messages, breaking on paragraph boundaries."""
    if len(body) <= limit:
        return [body]
    paragraphs = body.split("\n\n")
    messages: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            if current:
                messages.append(current)
            # If single paragraph exceeds limit, hard-split on newlines
            if len(para) > limit:
                lines = para.split("\n")
                current = ""
                for line in lines:
                    candidate = f"{current}\n{line}" if current else line
                    if len(candidate) > limit:
                        if current:
                            messages.append(current)
                            current = ""
                        if len(line) > limit:
                            chunks = _hard_split(line, limit)
                            messages.extend(chunks[:-1])
                            current = chunks[-1]
                        else:
                            current = line
                    else:
                        current = candidate
            else:
                current = para
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def build_detail_messages(
    sections: list[Section],
    limit: int,
) -> tuple[list[str], dict[int, int]]:
    """Build detail messages from sections.

    Returns (messages, section_start_map) where section_start_map maps
    section index → detail message index (0-based within details).
    """
    messages: list[str] = []
    section_starts: dict[int, int] = {}
    for i, section in enumerate(sections):
        section_starts[i] = len(messages)
        parts = split_body(section.body, limit)
        messages.extend(parts)
    return messages, section_starts


def _default_bullet(section: Section, url: str) -> str:
    """Default bullet format (Discord/Markdown): bold linked title."""
    return f"- [**{section.title}**]({url}) — {section.summary}"


def build_summary_partition(
    linked: LinkedThread,
    section_urls: list[str],
    limit: int,
    bullet_fn: Callable[[Section, str], str] = _default_bullet,
) -> tuple[list[str], list[MessageAtoms]]:
    """Greedy-pack summary bullets; return (messages, partition).

    Raises ``ValueError`` if any single bullet exceeds ``limit`` — callers must
    keep individual bullets under the platform's message limit. Hard-splitting
    a bullet inside a partition-preserving packer would create a chunk-count
    discrepancy between phase-1 (placeholder URLs) and phase-4 (real URLs),
    which is exactly the failure mode this partition tracking exists to
    prevent.
    """
    bullets: list[str] = []
    for i, section in enumerate(linked.sections):
        bullet = bullet_fn(section, section_urls[i])
        if len(bullet) > limit:
            raise ValueError(
                f"Section {i} ({section.title!r}) bullet is {len(bullet)} chars, "
                f"exceeds limit {limit}; shorten summary/title."
            )
        bullets.append(bullet)

    messages: list[str] = []
    partition: list[MessageAtoms] = []
    current = ""
    atoms = MessageAtoms(bullet_indices=[])

    if linked.summary_prefix:
        current = linked.summary_prefix
        atoms.has_prefix = True

    for i, bullet in enumerate(bullets):
        candidate = f"{current}\n{bullet}" if current else bullet
        if len(candidate) > limit:
            messages.append(current)
            partition.append(atoms)
            current = bullet
            atoms = MessageAtoms(bullet_indices=[i])
        else:
            current = candidate
            atoms.bullet_indices.append(i)

    if linked.summary_suffix:
        candidate = f"{current}\n{linked.summary_suffix}" if current else linked.summary_suffix
        if len(candidate) > limit:
            messages.append(current)
            partition.append(atoms)
            current = linked.summary_suffix
            atoms = MessageAtoms(bullet_indices=[], has_suffix=True)
        else:
            current = candidate
            atoms.has_suffix = True

    if current:
        messages.append(current)
        partition.append(atoms)

    return messages, partition


def render_summary_from_partition(
    linked: LinkedThread,
    section_urls: list[str],
    partition: list[MessageAtoms],
    bullet_fn: Callable[[Section, str], str] = _default_bullet,
) -> list[str]:
    """Rebuild summary messages from a fixed partition using (real) URLs.

    Message count is ``len(partition)`` by construction. When ``section_urls``
    are ≤ the placeholder-URL length used to compute the partition, every
    rebuilt message stays ≤ the original limit.
    """
    messages: list[str] = []
    for atoms in partition:
        parts: list[str] = []
        if atoms.has_prefix:
            parts.append(linked.summary_prefix)
        for i in atoms.bullet_indices:
            parts.append(bullet_fn(linked.sections[i], section_urls[i]))
        if atoms.has_suffix:
            parts.append(linked.summary_suffix)
        messages.append("\n".join(parts))
    return messages


def build_summary_messages(
    linked: LinkedThread,
    section_urls: list[str],
    limit: int,
    bullet_fn: Callable[[Section, str], str] = _default_bullet,
) -> list[str]:
    """Greedy-pack summary bullets into messages. Convenience wrapper.

    See ``build_summary_partition`` for the partition-returning variant used
    by ``sync_linked`` to preserve message count across the placeholder→real
    URL substitution.
    """
    messages, _ = build_summary_partition(linked, section_urls, limit, bullet_fn)
    return messages
