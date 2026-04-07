"""Tests for linked summary thread logic."""
from thrds import LinkedThread, Section
from thrds.linked import build_detail_messages, build_summary_messages, split_body


def test_split_body_under_limit():
    assert split_body("short text", 100) == ["short text"]


def test_split_body_on_paragraphs():
    body = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    result = split_body(body, 30)
    assert result == ["Paragraph 1\n\nParagraph 2", "Paragraph 3"]


def test_split_body_hard_split():
    body = "Line 1\nLine 2\nLine 3"
    result = split_body(body, 14)
    assert result == ["Line 1\nLine 2", "Line 3"]


def test_build_detail_messages():
    sections = [
        Section(title="A", summary="a", body="Detail A"),
        Section(title="B", summary="b", body="Detail B part 1\n\nDetail B part 2"),
    ]
    msgs, starts = build_detail_messages(sections, 100)
    assert msgs == ["Detail A", "Detail B part 1\n\nDetail B part 2"]
    assert starts == {0: 0, 1: 1}


def test_build_detail_messages_with_split():
    sections = [
        Section(title="A", summary="a", body="Short A"),
        Section(title="B", summary="b", body="Part 1\n\nPart 2"),
    ]
    msgs, starts = build_detail_messages(sections, 10)
    assert msgs == ["Short A", "Part 1", "Part 2"]
    assert starts == {0: 0, 1: 1}


def test_build_summary_messages_single():
    linked = LinkedThread(
        summary_prefix="# Digest",
        sections=[
            Section(title="A", summary="stuff", body=""),
            Section(title="B", summary="things", body=""),
        ],
    )
    links = ["(link-a)", "(link-b)"]
    msgs = build_summary_messages(linked, links, 200)
    assert len(msgs) == 1
    assert "**A**" in msgs[0]
    assert "**B**" in msgs[0]
    assert "(link-a)" in msgs[0]


def test_build_summary_messages_split():
    linked = LinkedThread(
        summary_prefix="# Digest",
        sections=[
            Section(title="A", summary="stuff", body=""),
            Section(title="B", summary="things", body=""),
        ],
    )
    links = ["(link-a)", "(link-b)"]
    # Very small limit forces split
    msgs = build_summary_messages(linked, links, 50)
    assert len(msgs) == 2
    assert "**A**" in msgs[0]
    assert "**B**" in msgs[1]


def test_build_summary_with_suffix():
    linked = LinkedThread(
        summary_prefix="# Digest",
        sections=[Section(title="A", summary="stuff", body="")],
        summary_suffix="_footer_",
    )
    links = ["(link)"]
    msgs = build_summary_messages(linked, links, 200)
    assert len(msgs) == 1
    assert "_footer_" in msgs[0]


def test_linked_thread_end_to_end():
    """Integration test using MockClient to verify the full sync_linked flow."""
    from thrds import Message
    from thrds.linked import LinkedSyncResult

    class MockLinkedClient:
        """Minimal mock that tracks posts and edits."""
        def __init__(self):
            self.messages: dict[str, str] = {}  # id → content
            self._next_id = 1
            self.edits: list[tuple[str, str]] = []

        def _new_id(self) -> str:
            id_ = str(self._next_id)
            self._next_id += 1
            return id_

        def list_messages(self, thread_id: str) -> list[Message]:
            return []

        def post(self, content: str, thread_id: str | None = None) -> Message:
            msg_id = self._new_id()
            self.messages[msg_id] = content
            return Message(id=msg_id, content=content)

        def edit(self, message_id: str, content: str) -> Message:
            self.messages[message_id] = content
            self.edits.append((message_id, content))
            return Message(id=message_id, content=content)

        def delete(self, message_id: str) -> None:
            del self.messages[message_id]

    from thrds.core import SyncOptions, Thread, sync

    client = MockLinkedClient()
    linked = LinkedThread(
        summary_prefix="# Weekly",
        sections=[
            Section(title="Topic A", summary="3 items", body="Detail about A"),
            Section(title="Topic B", summary="5 items", body="Detail about B"),
        ],
    )

    # Simulate the sync_linked flow manually (platform-agnostic)
    detail_msgs, section_starts = build_detail_messages(linked.sections, 2000)
    placeholder = "[placeholder-link-xxxxx]"
    placeholder_links = [placeholder] * len(linked.sections)
    summary_msgs = build_summary_messages(linked, placeholder_links, 2000)

    n_summary = len(summary_msgs)
    all_msgs = summary_msgs + detail_msgs

    result = sync(client, Thread(messages=all_msgs))

    detail_ids = result.message_ids[n_summary:]
    summary_ids = result.message_ids[:n_summary]

    # Verify detail messages were posted
    assert len(detail_ids) == 2
    assert client.messages[detail_ids[0]] == "Detail about A"
    assert client.messages[detail_ids[1]] == "Detail about B"

    # Build real links and edit summaries
    real_links = [f"(link-to-{detail_ids[section_starts[i]]})" for i in range(len(linked.sections))]
    final_summaries = build_summary_messages(linked, real_links, 2000)

    for msg_id, content in zip(summary_ids, final_summaries):
        client.edit(msg_id, content)

    # Verify summary was edited with real links
    assert len(client.edits) == n_summary
    for msg_id in summary_ids:
        content = client.messages[msg_id]
        assert "placeholder" not in content
        assert "link-to-" in content
