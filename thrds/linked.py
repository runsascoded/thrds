"""Linked summary threads: summary messages with links to detail messages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


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


def build_summary_messages(
    linked: LinkedThread,
    section_urls: list[str],
    limit: int,
    bullet_fn: Callable[[Section, str], str] = _default_bullet,
) -> list[str]:
    """Build summary messages with section bullets and links.

    Greedy-packs bullets into messages respecting the char limit.
    section_urls[i] is the link URL for section i (placeholder or real).
    bullet_fn(section, url) returns the formatted bullet line.
    """
    # Pre-split any bullet that alone would exceed the limit — otherwise the
    # greedy packer emits an over-limit message, which fails at post/edit.
    bullets: list[list[str]] = []
    for i, section in enumerate(linked.sections):
        bullet = bullet_fn(section, section_urls[i])
        bullets.append(_hard_split(bullet, limit))

    messages: list[str] = []
    current = linked.summary_prefix

    for chunks in bullets:
        for chunk in chunks:
            if current:
                candidate = f"{current}\n{chunk}"
            else:
                candidate = chunk
            if len(candidate) > limit:
                if current:
                    messages.append(current)
                current = chunk
            else:
                current = candidate

    if linked.summary_suffix:
        candidate = f"{current}\n{linked.summary_suffix}"
        if len(candidate) > limit:
            messages.append(current)
            current = linked.summary_suffix
        else:
            current = candidate

    if current:
        messages.append(current)

    return messages
