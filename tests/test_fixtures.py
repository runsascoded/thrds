"""Parametrized tests driven by tests/fixtures/sync.json.

The JSON fixtures are the contract for the diff/edit/post/delete sync
algorithm — they are consumed by both this Python suite and the
TypeScript impl (`ts` branch). Keep this file in sync with the spec
form. Algorithm-only cases live in the JSON; tests that exercise
language-specific surface (rate-limit exceptions, ANSI-colored
preview output, etc.) stay in test_sync.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from thrds import ActionType, Message, Thread, sync


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sync.json"


@dataclass
class FixtureClient:
    """In-memory thrds client. Enforces the `editable` contract: edit
    and delete on non-editable messages raise rather than silently
    corrupting state. Assigns new ids `n0`, `n1`, ... to posted
    messages so fixture assertions stay deterministic."""

    threads: dict[str, list[Message]]
    _new_id_counter: int = 0

    def _new_id(self) -> str:
        id_ = f"n{self._new_id_counter}"
        self._new_id_counter += 1
        return id_

    def list_messages(self, thread_id: str) -> list[Message]:
        return list(self.threads.get(thread_id, []))

    def post(self, content: str, thread_id: str | None = None) -> Message:
        msg = Message(id=self._new_id(), content=content, editable=True)
        if thread_id is None:
            self.threads[msg.id] = [msg]
        else:
            self.threads.setdefault(thread_id, []).append(msg)
        return msg

    def edit(self, message_id: str, content: str) -> Message:
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    assert m.editable, f"edit() called on non-editable {message_id}"
                    msgs[i] = Message(id=message_id, content=content, editable=True)
                    return msgs[i]
        raise ValueError(f"Message {message_id} not found")

    def delete(self, message_id: str) -> None:
        for msgs in self.threads.values():
            for i, m in enumerate(msgs):
                if m.id == message_id:
                    assert m.editable, f"delete() called on non-editable {message_id}"
                    msgs.pop(i)
                    return
        raise ValueError(f"Message {message_id} not found")


def load_cases() -> list[dict]:
    with FIXTURE_PATH.open() as f:
        return json.load(f)["cases"]


def _build_client(case: dict) -> FixtureClient:
    thread_id = case["thread_id"]
    existing = [
        Message(id=m["id"], content=m["content"], editable=m.get("editable", True))
        for m in case["existing"]
    ]
    threads = {thread_id: existing} if thread_id is not None else {}
    return FixtureClient(threads=threads)


def _check_action(actual, expected: dict) -> None:
    assert actual.type.name == expected["type"], (
        f"action.type: expected {expected['type']}, got {actual.type.name}"
    )
    assert actual.index == expected["index"]
    if "message_id" in expected:
        assert actual.message_id == expected["message_id"]
    if "content" in expected:
        assert actual.content == expected["content"]
    if "prior_content" in expected:
        assert actual.prior_content == expected["prior_content"]


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["name"])
def test_sync_fixture(case: dict) -> None:
    client = _build_client(case)
    desired = Thread(messages=case["desired"])
    result = sync(client, desired, thread_id=case["thread_id"])

    expected_actions = case["expected_actions"]
    assert len(result.actions) == len(expected_actions), (
        f"action count: expected {len(expected_actions)}, got {len(result.actions)}\n"
        f"  expected: {[a['type'] for a in expected_actions]}\n"
        f"  actual:   {[a.type.name for a in result.actions]}"
    )
    for actual, expected in zip(result.actions, expected_actions):
        _check_action(actual, expected)

    if "expected_message_ids" in case:
        assert result.message_ids == case["expected_message_ids"]

    final_thread_id = result.thread_id
    final_msgs = client.threads.get(final_thread_id, [])

    if "expected_final" in case:
        actual_final = [
            {"id": m.id, "content": m.content, "editable": m.editable}
            for m in final_msgs
        ]
        assert actual_final == case["expected_final"]
    elif "expected_final_contents" in case:
        assert [m.content for m in final_msgs] == case["expected_final_contents"]


def test_fixture_file_well_formed() -> None:
    """Sanity: every case has the required fields, action types are valid,
    `expected_final` and `expected_final_contents` aren't both set."""
    valid_action_types = {t.name for t in ActionType}
    cases = load_cases()
    assert cases, "no fixture cases found"
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), f"duplicate case names: {names}"
    for c in cases:
        assert set(c) >= {"name", "thread_id", "existing", "desired", "expected_actions"}
        for a in c["expected_actions"]:
            assert a["type"] in valid_action_types, f"{c['name']}: unknown action type {a['type']!r}"
            assert "index" in a
        has_final = "expected_final" in c
        has_contents = "expected_final_contents" in c
        assert not (has_final and has_contents), (
            f"{c['name']}: set exactly one of expected_final / expected_final_contents"
        )
