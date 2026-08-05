"""Tests for the multi-thread doc .md format (parser + serializer)."""
from __future__ import annotations

from pathlib import Path

import pytest

from thrds import Doc, DocMessage, DocThread, Frontmatter
from thrds.md import ParsedDoc, parse_doc, serialize_doc


FIXTURE = Path(__file__).parent / "fixtures" / "five_thread_post.md"


def _mine(*contents: str) -> list[DocMessage]:
    """Compact builder: [DocMessage(c) for c in contents] with author=None."""
    return [DocMessage(content=c) for c in contents]


EXPECTED_PREAMBLE = (
    "Sorry for the delay @stakeholder, updates below (adapted from "
    "[source](https://example.com), lmk if anything doesn't make sense):"
)


EXPECTED_THREADS = [
    DocThread(
        slug="mfu",
        messages=[
            DocMessage(content="1. Latest MFU (still 6%, but chip-wide now)"),
            DocMessage(content="Previously-quoted ≈6% was one core of four; now measured across the full chip."),
            DocMessage(content="Interesting — is the 6% stable across runs, or is there variance?", author="grayjh"),
            DocMessage(content="See the [profiling thread](#profiling) for methodology."),
        ],
    ),
    DocThread(
        slug="tflops-q",
        messages=_mine(
            "2. Q: what per-core TFLOP/s should I divide by for MFU?",
            "Bogus values from datasheet vs measured differ ~15%; see [MFU thread](#mfu) for the numerator.",
        ),
    ),
    DocThread(
        slug="profiling",
        messages=_mine(
            "3. Profiling / improving MFU",
            "Currently at 6%; target is 20%+.",
            "Two candidate bottlenecks under investigation.",
        ),
    ),
    DocThread(
        slug="ce",
        messages=_mine(
            "4. `bogus-library`, plugin integration?",
            "Blocker: no upstream tags.",
            "Proposed workaround in the [segfault thread](#segfault).",
            "ETA next week.",
        ),
    ),
    DocThread(
        slug="segfault",
        messages=_mine(
            "5. `bogus-compiler` segfault in `lowering-pass`",
            "- MWE gist: <https://example.com/gist>\n- One flag flips crash → compile.",
        ),
    ),
]


EXPECTED_FRONTMATTER = Frontmatter(
    channel="C0BOGUS0001",
    thread_ts=None,
    session_id="fixture-uuid-0000",
)


def test_parse_fixture_shape():
    """The fixture parses to exactly the expected Doc + frontmatter."""
    parsed = parse_doc(FIXTURE.read_text())
    assert parsed == ParsedDoc(
        doc=Doc(preamble=EXPECTED_PREAMBLE, threads=EXPECTED_THREADS),
        frontmatter=EXPECTED_FRONTMATTER,
    )


def test_fixture_is_canonical():
    """The fixture is already in serialize_doc's canonical form (fixed point)."""
    parsed = parse_doc(FIXTURE.read_text())
    assert serialize_doc(parsed.doc, parsed.frontmatter) == FIXTURE.read_text()


def test_round_trip():
    """parse ∘ serialize is identity on the Doc + frontmatter."""
    parsed = parse_doc(FIXTURE.read_text())
    round_tripped = parse_doc(serialize_doc(parsed.doc, parsed.frontmatter))
    assert round_tripped == parsed


def test_serialize_without_frontmatter():
    """serialize_doc(doc) omits the `---` block when frontmatter is None."""
    doc = Doc(
        preamble="Hi.",
        threads=[DocThread(slug="a", messages=_mine("OP a."))],
    )
    assert serialize_doc(doc) == "Hi.\n\n=== a\n\nOP a.\n"


def test_serialize_empty_frontmatter_omitted():
    """An all-None Frontmatter serializes to no `---` block."""
    doc = Doc(preamble="Hi.", threads=[DocThread(slug="a", messages=_mine("OP."))])
    assert serialize_doc(doc, Frontmatter()) == "Hi.\n\n=== a\n\nOP.\n"


def test_no_preamble():
    """A doc starting straight with `===` parses with preamble=None."""
    parsed = parse_doc("=== a\n\nOP a.\n")
    assert parsed == ParsedDoc(
        doc=Doc(preamble=None, threads=[DocThread(slug="a", messages=_mine("OP a."))]),
        frontmatter=Frontmatter(),
    )


def test_thread_without_slug():
    """A bare `===` (no slug) parses to DocThread with slug=None."""
    parsed = parse_doc("===\n\nOP with no slug.\n")
    assert parsed.doc.threads == [DocThread(slug=None, messages=_mine("OP with no slug."))]


def test_multi_paragraph_message():
    """Internal blank lines within a message are preserved as paragraph breaks."""
    text = "=== a\n\nPara 1.\n\nPara 2.\n\n+++\n\nReply.\n"
    parsed = parse_doc(text)
    assert parsed.doc.threads == [
        DocThread(slug="a", messages=_mine("Para 1.\n\nPara 2.", "Reply.")),
    ]
    # Canonical form should round-trip byte-exactly.
    assert serialize_doc(parsed.doc) == text


def test_foreign_reply_parses_with_author():
    """`+++ @alice` parses to DocMessage(content=..., author='alice')."""
    text = "=== a\n\nOP.\n\n+++ @alice\n\nReply from alice.\n"
    parsed = parse_doc(text)
    assert parsed.doc.threads == [
        DocThread(slug="a", messages=[
            DocMessage(content="OP.", author=None),
            DocMessage(content="Reply from alice.", author="alice"),
        ]),
    ]


def test_foreign_reply_round_trips():
    """Serializer emits `+++ @author`; parse ∘ serialize is identity on author-tagged messages."""
    text = "=== a\n\nOP.\n\n+++\n\nOur reply.\n\n+++ @alice\n\nHers.\n\n+++ @bob.jr\n\nHis.\n\n+++\n\nOurs again.\n"
    parsed = parse_doc(text)
    assert serialize_doc(parsed.doc) == text
    assert parsed.doc.threads[0].messages == [
        DocMessage(content="OP.", author=None),
        DocMessage(content="Our reply.", author=None),
        DocMessage(content="Hers.", author="alice"),
        DocMessage(content="His.", author="bob.jr"),
        DocMessage(content="Ours again.", author=None),
    ]


def test_foreign_op_raises_on_serialize():
    """Top-level = ours by definition. A DocMessage(author=X) as OP is a data-model bug."""
    bad = Doc(threads=[DocThread(slug="a", messages=[DocMessage("OP.", author="alice")])])
    with pytest.raises(ValueError, match="OP author must be None"):
        serialize_doc(bad)


def test_duplicate_slug_raises():
    """Two threads with the same slug are ambiguous for cross-refs — reject."""
    with pytest.raises(ValueError, match="Duplicate thread slug: 'a'"):
        parse_doc("=== a\n\nOP1.\n\n=== a\n\nOP2.\n")


def test_empty_op_raises():
    """A thread whose OP body is empty is malformed."""
    with pytest.raises(ValueError, match="OP .* is empty"):
        parse_doc("=== a\n\n+++\n\nReply only.\n")


def test_empty_reply_raises():
    """A `+++` with no body before the next boundary is malformed."""
    with pytest.raises(ValueError, match="reply 1 is empty"):
        parse_doc("=== a\n\nOP.\n\n+++\n\n+++\n\nReply 2.\n")


def test_unclosed_frontmatter_raises():
    """A `---` at the top without a closing delimiter is malformed."""
    with pytest.raises(ValueError, match="no closing delimiter"):
        parse_doc("---\nchannel: C\n\n=== a\n\nOP.\n")


def test_unknown_frontmatter_key_raises():
    """Frontmatter with keys outside the known set is rejected."""
    with pytest.raises(ValueError, match=r"Unknown frontmatter keys: \['bogus'\]"):
        parse_doc("---\nbogus: value\n---\n\n=== a\n\nOP.\n")


def test_cross_refs_preserved_verbatim():
    """`[text](#slug)` cross-refs are opaque to the parser — plain text in the message body.

    Resolution happens later (Phase E). This test just pins that the parser
    doesn't mangle or interpret them.
    """
    parsed = parse_doc(FIXTURE.read_text())
    mfu = parsed.doc.threads[0]
    assert mfu.messages[3].content == "See the [profiling thread](#profiling) for methodology."


def test_parent_ts_default_is_none():
    """DocThread.parent_ts is the design hook for the reply-to-others case; defaults to None."""
    parsed = parse_doc("=== a\n\nOP.\n")
    assert parsed.doc.threads[0].parent_ts is None
