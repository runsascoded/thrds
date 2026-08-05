"""Tests for `thrds/refs.py` — cross-thread `#slug` ref helpers."""
from __future__ import annotations

import pytest

from thrds import Doc, DocMessage, DocThread
from thrds.refs import (
    CROSS_REF_RE,
    PLACEHOLDER_URL,
    doc_has_refs,
    find_refs,
    substitute_doc_refs,
    substitute_refs,
    thread_has_refs,
    validate_refs,
)


# --- regex ---

def test_placeholder_url_is_180_chars():
    """Placeholder matches linked.py's upper-bound convention."""
    assert len(PLACEHOLDER_URL) == 180


def test_regex_matches_slug_form_only():
    """`(#slug)` matches; `(https://…)` does not."""
    assert CROSS_REF_RE.findall("[a](#mfu)") == [("a", "mfu")]
    assert CROSS_REF_RE.findall("[a](https://x.y)") == []
    assert CROSS_REF_RE.findall("[a](mfu)") == []  # missing leading #


def test_regex_accepts_slug_charset():
    """Slug charset = alphanumeric + underscore + hyphen (matches `.md` parser)."""
    assert CROSS_REF_RE.findall("[a](#foo-bar_1)") == [("a", "foo-bar_1")]
    assert CROSS_REF_RE.findall("[a](#foo.bar)") == []  # dot excluded


# --- find_refs ---

def test_find_refs_returns_text_and_slug_pairs_in_order():
    text = "See [MFU](#mfu) and [profiling](#profiling) for details."
    assert find_refs(text) == [("MFU", "mfu"), ("profiling", "profiling")]


def test_find_refs_empty_string_returns_empty_list():
    assert find_refs("") == []


def test_find_refs_ignores_plain_markdown_links():
    text = "See [MFU docs](https://example.com) — not a cross-ref."
    assert find_refs(text) == []


# --- substitute_refs ---

def test_substitute_refs_rewrites_each_ref_via_resolver():
    text = "See [MFU](#mfu) and [prof](#profiling)."
    resolver = lambda slug: f"https://slack.example/{slug}"
    out = substitute_refs(text, resolver)
    assert out == "See [MFU](https://slack.example/mfu) and [prof](https://slack.example/profiling)."


def test_substitute_refs_preserves_non_ref_content():
    text = "Regular text — no refs here. [external](https://x.y) stays put."
    assert substitute_refs(text, lambda _: "unused") == text


# --- substitute_doc_refs ---

def test_substitute_doc_refs_walks_preamble_and_all_messages():
    doc = Doc(
        preamble="See [MFU thread](#mfu).",
        threads=[
            DocThread(slug="mfu", messages=[
                DocMessage("OP — see [prof](#prof)."),
                DocMessage("Reply."),
            ]),
            DocThread(slug="prof", messages=[
                DocMessage("Prof OP — see [MFU](#mfu)."),
            ]),
        ],
    )
    out = substitute_doc_refs(doc, lambda slug: f"URL:{slug}")
    assert out == Doc(
        preamble="See [MFU thread](URL:mfu).",
        threads=[
            DocThread(slug="mfu", messages=[
                DocMessage("OP — see [prof](URL:prof)."),
                DocMessage("Reply."),
            ]),
            DocThread(slug="prof", messages=[
                DocMessage("Prof OP — see [MFU](URL:mfu)."),
            ]),
        ],
    )


def test_substitute_doc_refs_is_pure():
    """Original doc is not mutated."""
    doc = Doc(threads=[DocThread(slug="a", messages=[DocMessage("See [x](#b).")])])
    _ = substitute_doc_refs(doc, lambda _: "X")
    assert doc.threads[0].messages[0].content == "See [x](#b)."


def test_substitute_doc_refs_preserves_message_author():
    doc = Doc(threads=[DocThread(slug="a", messages=[
        DocMessage("OP.", author=None),
        DocMessage("Reply — [x](#a).", author="alice"),
    ])])
    out = substitute_doc_refs(doc, lambda slug: f"URL:{slug}")
    assert [m.author for m in out.threads[0].messages] == [None, "alice"]
    assert out.threads[0].messages[1].content == "Reply — [x](URL:a)."


# --- validate_refs ---

def test_validate_refs_ok_when_all_slugs_exist():
    doc = Doc(threads=[
        DocThread(slug="a", messages=[DocMessage("See [x](#b).")]),
        DocThread(slug="b", messages=[DocMessage("OP b.")]),
    ])
    validate_refs(doc)  # no raise


def test_validate_refs_raises_on_dangling_ref():
    doc = Doc(threads=[
        DocThread(slug="a", messages=[DocMessage("See [x](#nope).")]),
    ])
    with pytest.raises(ValueError, match=r"thread 'a', msg 0: #nope"):
        validate_refs(doc)


def test_validate_refs_lists_every_dangling_ref():
    doc = Doc(
        preamble="See [x](#bogus1).",
        threads=[
            DocThread(slug="a", messages=[
                DocMessage("First — [x](#bogus2)."),
                DocMessage("Second — [y](#bogus3)."),
            ]),
        ],
    )
    with pytest.raises(ValueError) as exc:
        validate_refs(doc)
    msg = str(exc.value)
    # Every dangling ref is called out; the caller can fix them all in one pass.
    assert "preamble: #bogus1" in msg
    assert "thread 'a', msg 0: #bogus2" in msg
    assert "thread 'a', msg 1: #bogus3" in msg


# --- has_refs helpers ---

def test_doc_has_refs_true_when_any_message_has_ref():
    doc = Doc(threads=[
        DocThread(slug="a", messages=[DocMessage("Plain.")]),
        DocThread(slug="b", messages=[DocMessage("OP with [x](#a).")]),
    ])
    assert doc_has_refs(doc) is True


def test_doc_has_refs_true_when_preamble_has_ref():
    doc = Doc(preamble="See [x](#a).", threads=[
        DocThread(slug="a", messages=[DocMessage("OP.")]),
    ])
    assert doc_has_refs(doc) is True


def test_doc_has_refs_false_when_no_refs():
    doc = Doc(
        preamble="Plain preamble.",
        threads=[DocThread(slug="a", messages=[DocMessage("OP.")])],
    )
    assert doc_has_refs(doc) is False


def test_thread_has_refs_scoped_to_the_one_thread():
    t_with = DocThread(slug="a", messages=[DocMessage("See [x](#b).")])
    t_without = DocThread(slug="b", messages=[DocMessage("Plain.")])
    assert thread_has_refs(t_with) is True
    assert thread_has_refs(t_without) is False
