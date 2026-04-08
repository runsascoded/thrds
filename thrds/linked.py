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
    bullets: list[str] = []
    for i, section in enumerate(linked.sections):
        bullet = bullet_fn(section, section_urls[i])
        bullets.append(bullet)

    messages: list[str] = []
    current = linked.summary_prefix

    for bullet in bullets:
        if current:
            candidate = f"{current}\n{bullet}"
        else:
            candidate = bullet
        if len(candidate) > limit:
            if current:
                messages.append(current)
            current = bullet
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
