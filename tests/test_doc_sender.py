"""Per-message sender in the thread doc (specs/doc-sender-syntax.md).

Named sender profiles in frontmatter (`sender.<name>.name` / `.avatar`),
referenced by the OP via `op_sender` and by replies via `+++ as <name>`.
`resolve_messages` turns them into `sync`'s `Msg` desired-input.
"""
from __future__ import annotations

import pytest

from thrds.core import Msg
from thrds.doc import Frontmatter, SenderProfile
from thrds.md import parse_thread, resolve_messages, serialize_thread

DOC = """\
---
channel: C123
op_sender: gcs-op
sender.gcs-op.name: GCS usage — 2026-09-14
sender.gcs-op.avatar: https://gcs.pages.dev/c.png
sender.gcs.name: GCS usage
sender.gcs.avatar: https://gcs.pages.dev/c.png
sender.alice.avatar: :smile:
---
Daily summary.

+++ as gcs
Teams: 42

+++ as alice
Alice note.
"""


def test_parse_populates_senders_and_refs():
    p = parse_thread(DOC)
    assert p.frontmatter.op_sender == "gcs-op"
    assert p.frontmatter.senders == {
        "gcs-op": SenderProfile(name="GCS usage — 2026-09-14", icon_url="https://gcs.pages.dev/c.png"),
        "gcs": SenderProfile(name="GCS usage", icon_url="https://gcs.pages.dev/c.png"),
        "alice": SenderProfile(name=None, icon_emoji=":smile:"),
    }
    assert [m.sender for m in p.thread.messages] == [None, "gcs", "alice"]


def test_round_trip_is_stable():
    p = parse_thread(DOC)
    reparsed = parse_thread(serialize_thread(p.thread, p.frontmatter))
    assert (reparsed.thread, reparsed.frontmatter) == (p.thread, p.frontmatter)


def test_serialize_canonical_form():
    p = parse_thread(DOC)
    assert serialize_thread(p.thread, p.frontmatter).split("\n") == [
        "---",
        "channel: C123",
        "op_sender: gcs-op",
        "sender.alice.avatar: :smile:",
        "sender.gcs.name: GCS usage",
        "sender.gcs.avatar: https://gcs.pages.dev/c.png",
        "sender.gcs-op.name: GCS usage — 2026-09-14",
        "sender.gcs-op.avatar: https://gcs.pages.dev/c.png",
        "---",
        "",
        "Daily summary.",
        "",
        "+++ as gcs",
        "",
        "Teams: 42",
        "",
        "+++ as alice",
        "",
        "Alice note.",
        "",
    ]


def test_resolve_messages_builds_msgs():
    p = parse_thread(DOC)
    assert resolve_messages(p.thread.messages, p.frontmatter) == [
        Msg("Daily summary.", username="GCS usage — 2026-09-14", icon_url="https://gcs.pages.dev/c.png"),
        Msg("Teams: 42", username="GCS usage", icon_url="https://gcs.pages.dev/c.png"),
        Msg("Alice note.", username=None, icon_emoji=":smile:"),
    ]


def test_sender_free_doc_resolves_to_bare_strings():
    p = parse_thread("OP body\n\n+++\n\nreply one\n")
    resolved = resolve_messages(p.thread.messages, p.frontmatter)
    assert resolved == ["OP body", "reply one"]
    assert all(isinstance(m, str) for m in resolved)


def test_lift_images_default_off_keeps_content():
    # Without lift_images, a trailing image line stays in content (Slack's form).
    p = parse_thread("OP body\n\n![plot](https://x/p.png)\n")
    assert resolve_messages(p.thread.messages, p.frontmatter) == [
        "OP body\n\n![plot](https://x/p.png)",
    ]


def test_lift_images_url_into_msg():
    from thrds.core import Image

    p = parse_thread("OP body\n\n![plot](https://x/p.png){bust}\n")
    assert resolve_messages(p.thread.messages, p.frontmatter, lift_images=True) == [
        Msg("OP body", images=[Image(url="https://x/p.png", alt="plot", bust=True)]),
    ]


def test_lift_images_local_path_resolved_against_base_dir():
    from pathlib import Path

    from thrds.core import Image

    p = parse_thread("caption\n\n![c](./plot.png)\n")
    assert resolve_messages(
        p.thread.messages, p.frontmatter, lift_images=True, base_dir=Path("/docs"),
    ) == [
        Msg("caption", images=[Image(path=Path("/docs/plot.png"), alt="c")]),
    ]


def test_lift_images_with_sender_keeps_both():
    from thrds.core import Image

    p = parse_thread(
        "---\nop_sender: gcs\nsender.gcs.name: GCS\n---\n"
        "OP body\n\n![plot](https://x/p.png)\n"
    )
    assert resolve_messages(p.thread.messages, p.frontmatter, lift_images=True) == [
        Msg("OP body", username="GCS", images=[Image(url="https://x/p.png", alt="plot")]),
    ]


def test_lift_images_mid_message_not_lifted():
    # Only a trailing image run lifts; an image with text after it stays inline.
    p = parse_thread("before\n\n![c](https://x/p.png)\n\nafter\n")
    resolved = resolve_messages(p.thread.messages, p.frontmatter, lift_images=True)
    assert resolved == ["before\n\n![c](https://x/p.png)\n\nafter"]


def test_emoji_vs_url_avatar_detection():
    p = parse_thread(
        "---\n"
        "sender.e.avatar: :tada:\n"
        "sender.u.avatar: https://x/y.png\n"
        "---\n"
        "OP\n\n+++ as e\ne\n\n+++ as u\nu\n"
    )
    assert p.frontmatter.senders == {
        "e": SenderProfile(icon_emoji=":tada:"),
        "u": SenderProfile(icon_url="https://x/y.png"),
    }


def test_undefined_reply_ref_raises():
    with pytest.raises(ValueError, match=r"Undefined sender ref\(s\): \['ghost'\]"):
        parse_thread("OP\n\n+++ as ghost\nx\n")


def test_undefined_op_sender_raises():
    with pytest.raises(ValueError, match=r"Undefined sender ref\(s\): \['nope'\]"):
        parse_thread("---\nop_sender: nope\n---\nOP\n")


def test_author_and_sender_same_delimiter_is_malformed():
    with pytest.raises(ValueError, match="malformed reply delimiter"):
        parse_thread("OP\n\n+++ @alice as gcs\nx\n")


def test_op_sender_in_multithread_doc_raises():
    from thrds.md import parse_doc

    with pytest.raises(ValueError, match="only supported in per-thread files"):
        parse_doc("---\nop_sender: gcs\n---\n=== t1\nOP\n")


def test_serialize_rejects_op_with_sender():
    from thrds.doc import DocMessage, DocThread

    t = DocThread(messages=[DocMessage(content="OP", sender="gcs")], slug="a")
    with pytest.raises(ValueError, match="OP author/sender must be None"):
        serialize_thread(t, Frontmatter())
