"""Tests for legacy-doc → per-thread-file migration (`thrds.migrate`)."""
from __future__ import annotations

import pytest

from thrds import Doc, DocMessage, DocThread, Frontmatter, SessionState, ThreadEntry, ThreadTarget
from thrds.md import parse_doc, parse_thread
from thrds.migrate import apply_migration, plan_migration


def _doc(*specs: tuple[str, list[str]], preamble: str | None = None) -> Doc:
    """Build a Doc from `(slug, [op, *replies])` pairs."""
    return Doc(
        threads=[
            DocThread(messages=[DocMessage(content=c) for c in contents], slug=slug)
            for slug, contents in specs
        ],
        preamble=preamble,
    )


# --- planning: file layout ---


def test_plan_numbers_threads_from_one():
    doc = _doc(('alpha', ['A.']), ('beta', ['B.']), ('gamma', ['C.']))
    plan = plan_migration(doc, SessionState.new(), 'draft.md')
    assert plan.filenames == ['01-alpha.md', '02-beta.md', '03-gamma.md']


def test_plan_preserves_doc_order_not_slug_order():
    doc = _doc(('zebra', ['Z.']), ('alpha', ['A.']))
    plan = plan_migration(doc, SessionState.new(), 'draft.md')
    assert plan.filenames == ['01-zebra.md', '02-alpha.md']


def test_plan_session_slug_from_doc_basename():
    plan = plan_migration(_doc(('a', ['A.'])), SessionState.new(), 'cw-quickwins.md')
    assert plan.session_slug == 'cw-quickwins'


def test_plan_session_slug_prefers_explicit_state_value():
    state = SessionState.new(session_slug='pinned')
    plan = plan_migration(_doc(('a', ['A.'])), state, 'cw-quickwins.md')
    assert plan.session_slug == 'pinned'


# --- planning: content preservation ---


def test_plan_thread_text_is_op_only():
    plan = plan_migration(_doc(('a', ['Just the OP.'])), SessionState.new(), 'd.md')
    assert plan.threads[0].text == 'Just the OP.\n'


def test_plan_thread_text_keeps_replies():
    plan = plan_migration(_doc(('a', ['OP.', 'R1.', 'R2.'])), SessionState.new(), 'd.md')
    assert plan.threads[0].text == 'OP.\n\n+++\n\nR1.\n\n+++\n\nR2.\n'


def test_plan_round_trips_through_parse_thread():
    """The written file must parse back to the same messages it came from."""
    doc = _doc(('a', ['OP body.', 'A reply.']))
    plan = plan_migration(doc, SessionState.new(), 'd.md')
    assert parse_thread(plan.threads[0].text, slug='a').thread == doc.threads[0]


def test_plan_content_matches_original_doc_bytes():
    """End-to-end: parse a real multi-thread doc, migrate, and confirm each
    thread's body survives verbatim."""
    text = "=== one\n\nFirst message.\n\n+++\n\nIts reply.\n\n=== two\n\nSecond message.\n"
    parsed = parse_doc(text)
    plan = plan_migration(parsed.doc, SessionState.new(), 'd.md')
    assert [t.text for t in plan.threads] == [
        'First message.\n\n+++\n\nIts reply.\n',
        'Second message.\n',
    ]


# --- planning: preamble ---


def test_plan_preamble_becomes_index_zero_thread():
    doc = _doc(('a', ['A.']), preamble='Header text.')
    plan = plan_migration(doc, SessionState.new(), 'd.md')
    assert plan.filenames == ['00-preamble.md', '01-a.md']


def test_plan_preamble_content_preserved():
    doc = _doc(('a', ['A.']), preamble='Header text.')
    plan = plan_migration(doc, SessionState.new(), 'd.md')
    assert plan.threads[0].text == 'Header text.\n'


def test_plan_no_preamble_file_when_absent():
    plan = plan_migration(_doc(('a', ['A.'])), SessionState.new(), 'd.md')
    assert plan.filenames == ['01-a.md']


def test_plan_preamble_slug_collision_raises():
    doc = _doc(('preamble', ['A.']), preamble='Header.')
    with pytest.raises(ValueError) as e:
        plan_migration(doc, SessionState.new(), 'd.md')
    assert str(e.value) == (
        "Cannot migrate: doc has a preamble and a thread already named "
        "'preamble'; rename that thread before migrating"
    )


# --- planning: state derivation ---


def test_plan_carries_staging_ts_into_entry():
    state = SessionState.new(staging_threads={'a': '1786840558.331079'})
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    assert plan.threads_map['a'] == ThreadEntry(
        staging_ts='1786840558.331079', target=None, state='draft',
    )


def test_plan_posted_thread_migrates_as_posted_with_pinned_target():
    state = SessionState.new(
        staging_threads={'a': '1.1'},
        prod_threads={'C0PROD': {'a': '9.9'}},
    )
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    assert plan.threads_map['a'] == ThreadEntry(
        staging_ts='1.1',
        target=ThreadTarget(channel='C0PROD'),
        state='posted',
        posted_ts='9.9',
    )


def test_plan_unposted_thread_is_draft_with_no_target():
    state = SessionState.new(prod_channel='C0DEFAULT', staging_threads={'a': '1.1'})
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    entry = plan.threads_map['a']
    assert (entry.state, entry.target, entry.posted_ts) == ('draft', None, None)


def test_unposted_thread_still_resolves_session_default_after_migration():
    """Leaving `target=None` is deliberate: `prod_channel` remains the default,
    so the batch case needs no per-thread config."""
    state = SessionState.new(prod_channel='C0DEFAULT', staging_threads={'a': '1.1'})
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    state.threads = plan.threads_map
    assert state.target_for('a') == ThreadTarget(channel='C0DEFAULT')


def test_plan_frontmatter_channel_becomes_default_target():
    fm = Frontmatter(channel='C0FM', thread_ts='7.7')
    plan = plan_migration(_doc(('a', ['A.'])), SessionState.new(), 'd.md', fm)
    assert plan.threads_map['a'].target == ThreadTarget(channel='C0FM', thread_ts='7.7')


def test_plan_multi_channel_slug_raises():
    state = SessionState.new(prod_threads={'C0A': {'a': '1.1'}, 'C0B': {'a': '2.2'}})
    with pytest.raises(ValueError) as e:
        plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    assert str(e.value) == (
        "Thread 'a' was posted to multiple prod channels (C0A, C0B); "
        "the per-thread model allows one destination per thread — "
        "split it into separate threads before migrating"
    )


def test_plan_unslugged_thread_raises():
    doc = Doc(threads=[
        DocThread(messages=[DocMessage(content='A.')], slug='a'),
        DocThread(messages=[DocMessage(content='B.')], slug=None),
    ])
    with pytest.raises(ValueError) as e:
        plan_migration(doc, SessionState.new(), 'd.md')
    assert str(e.value) == (
        "Cannot migrate: thread(s) at position [1] have no `=== slug`; "
        "a slug is the thread's filename and its identity in `thrds.json` — "
        "add one to each before migrating"
    )


# --- apply ---


def test_apply_writes_thread_files(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n\n=== b\n\nB.\n")
    state = SessionState.new(doc_path='d.md')
    plan = plan_migration(parse_doc((tmp_path / 'd.md').read_text()).doc, state, 'd.md')
    apply_migration(tmp_path, state, plan)
    assert sorted(p.name for p in tmp_path.glob('*.md')) == ['01-a.md', '02-b.md']


def test_apply_removes_legacy_doc(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='d.md')
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    apply_migration(tmp_path, state, plan)
    assert not (tmp_path / 'd.md').exists()


def test_apply_updates_state_fields(tmp_path):
    (tmp_path / 'cw.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(
        doc_path='cw.md',
        staging_threads={'a': '1.1'},
        prod_threads={'C0P': {'a': '2.2'}},
        staging_preamble_ts='0.1',
        prod_preamble_ts={'C0P': '0.2'},
    )
    plan = plan_migration(_doc(('a', ['A.'])), state, 'cw.md')
    apply_migration(tmp_path, state, plan)
    assert (
        state.session_slug,
        state.doc_path,
        state.staging_threads,
        state.prod_threads,
        state.staging_preamble_ts,
        state.prod_preamble_ts,
    ) == ('cw', None, {}, {}, None, {})


def test_apply_populates_threads_map(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='d.md', staging_threads={'a': '1.1'})
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    apply_migration(tmp_path, state, plan)
    assert state.threads == {'a': ThreadEntry(staging_ts='1.1', state='draft')}


def test_apply_returns_touched_paths(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='d.md')
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    touched = apply_migration(tmp_path, state, plan)
    assert sorted(p.name for p in touched) == ['01-a.md', 'd.md']


def test_apply_refuses_when_thread_files_already_present(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    (tmp_path / '01-a.md').write_text('A.\n')
    state = SessionState.new(doc_path='d.md')
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    with pytest.raises(ValueError) as e:
        apply_migration(tmp_path, state, plan)
    assert str(e.value) == (
        f"Cannot migrate: {tmp_path} already has thread files "
        f"(01-a.md) — this session looks migrated"
    )


def test_apply_leaves_session_no_longer_legacy(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='d.md', staging_threads={'a': '1.1'})
    assert state.is_legacy is True
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    apply_migration(tmp_path, state, plan)
    assert state.is_legacy is False


def test_apply_preserves_staging_channel_name_across_migration(tmp_path):
    """`doc_slug` drives the staging PC name; retiring `doc_path` must not
    change it, or the session would look for a differently-named channel."""
    (tmp_path / 'cw-quickwins.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='cw-quickwins.md')
    before = state.staging_channel_name()
    plan = plan_migration(_doc(('a', ['A.'])), state, 'cw-quickwins.md')
    apply_migration(tmp_path, state, plan)
    assert state.staging_channel_name() == before


def test_apply_state_round_trips_through_save_load(tmp_path):
    (tmp_path / 'd.md').write_text("=== a\n\nA.\n")
    state = SessionState.new(doc_path='d.md', staging_threads={'a': '1.1'})
    plan = plan_migration(_doc(('a', ['A.'])), state, 'd.md')
    apply_migration(tmp_path, state, plan)
    state.save(tmp_path)
    assert SessionState.load(tmp_path).threads == state.threads
