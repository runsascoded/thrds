from __future__ import annotations

import json
import random
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode

from .core import Message, SyncOptions, SyncResult, Thread, sync
from .linked import LinkedSyncResult, LinkedThread, Section, build_detail_messages, build_summary_messages

SLACK_MESSAGE_LIMIT = 4000


class SlackClient:
    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel
        self._suppress_unfurls: bool = True
        self._metadata_by_content: dict[str, dict] | None = None
        self._skip_op: bool = False

    def _request(
        self,
        endpoint: str,
        data: dict | None = None,
        method: str = "POST",
    ) -> dict:
        url = f"https://slack.com/api/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
        }
        if method == "GET" and data:
            url = f"{url}?{urlencode(data)}"
            body = None
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                with urllib.request.urlopen(req) as resp:
                    result = json.loads(resp.read())
                break
            except HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    retry_after = int(e.headers.get("Retry-After", 1))
                    time.sleep(retry_after)
                    continue
                raise RuntimeError(f"Slack API error: {e.code} {e.read().decode()}") from e
        if not result.get("ok"):
            raise RuntimeError(f"Slack API error: {result.get('error', result)}")
        return result

    def _metadata_for(self, content: str) -> dict | None:
        """Look up metadata for a message by its content."""
        if self._metadata_by_content is None:
            return None
        return self._metadata_by_content.get(content)

    def list_messages(self, thread_id: str) -> list[Message]:
        result = self._request("conversations.replies", {
            "channel": self.channel,
            "ts": thread_id,
        }, method="GET")
        messages = [
            Message(id=m["ts"], content=m.get("text", ""))
            for m in result.get("messages", [])
        ]
        # In sync_linked mode, skip the OP (thread parent) — it's managed separately
        if self._skip_op and messages:
            messages = messages[1:]
        return messages

    def post(self, content: str, thread_id: str | None = None) -> Message:
        data: dict = {
            "channel": self.channel,
            "text": content,
            "unfurl_links": not self._suppress_unfurls,
            "unfurl_media": not self._suppress_unfurls,
        }
        if thread_id is not None:
            data["thread_ts"] = thread_id
        md = self._metadata_for(content)
        if md is not None:
            data["metadata"] = md
        result = self._request("chat.postMessage", data)
        return Message(id=result["ts"], content=content)

    def edit(self, message_id: str, content: str) -> Message:
        data: dict = {
            "channel": self.channel,
            "ts": message_id,
            "text": content,
            "unfurl_links": not self._suppress_unfurls,
            "unfurl_media": not self._suppress_unfurls,
        }
        md = self._metadata_for(content)
        if md is not None:
            data["metadata"] = md
        self._request("chat.update", data)
        return Message(id=message_id, content=content)

    def permalink(self, message_ts: str) -> str:
        """Get a permalink URL for a Slack message."""
        result = self._request("chat.getPermalink", {
            "channel": self.channel,
            "message_ts": message_ts,
        }, method="GET")
        return result["permalink"]

    def delete(self, message_id: str) -> None:
        self._request("chat.delete", {
            "channel": self.channel,
            "ts": message_id,
        })

    def sync(
        self,
        thread: Thread,
        thread_ts: str | None = None,
        dry_run: bool = False,
        pace: float = 0.4,
        jitter: float = 0.0,
        suppress_unfurls: bool = True,
        metadata: dict[str, dict] | None = None,
    ) -> SyncResult:
        """Sync a thread to the desired state.

        Args:
            metadata: Optional dict mapping message content → Slack metadata.
                Each matching message gets the metadata dict passed on post/edit.

                Example::

                    metadata={
                        crash_text: {
                            "event_type": "new_crash",
                            "event_payload": {"ACCID": "123"},
                        },
                    }
        """
        self._suppress_unfurls = suppress_unfurls
        self._metadata_by_content = metadata
        try:
            return sync(
                client=self,
                desired=thread,
                thread_id=thread_ts,
                options=SyncOptions(
                    dry_run=dry_run,
                    pace=pace,
                    jitter=jitter,
                    suppress_unfurls=suppress_unfurls,
                ),
            )
        finally:
            self._metadata_by_content = None

    @staticmethod
    def _bullet(section: Section, url: str) -> str:
        """Slack mrkdwn bullet: linked bold title."""
        return f"- <{url}|*{section.title}*> — {section.summary}"

    def _detail_url_placeholder(self) -> str:
        """Placeholder URL with max possible length for space reservation.

        Slack permalinks are ~120 chars; use 130 as safe upper bound.
        """
        return "x" * 130

    def sync_linked(
        self,
        linked: LinkedThread,
        thread_ts: str | None = None,
        dry_run: bool = False,
        pace: float = 0.4,
        jitter: float = 0.0,
        suppress_unfurls: bool = True,
    ) -> LinkedSyncResult:
        """Sync a linked summary thread.

        In Slack, the thread parent (OP) is the first message in
        conversations.replies. This method manages the OP separately
        (setting it to summary_prefix) and syncs bullets + details
        as thread replies starting at index 1.
        """
        placeholder = self._detail_url_placeholder()

        # Build summary bullets WITHOUT the prefix (prefix goes to OP)
        linked_replies = LinkedThread(
            summary_prefix="",
            sections=linked.sections,
            summary_suffix=linked.summary_suffix,
        )

        # Phase 1: Build detail + summary messages with placeholder links
        detail_msgs, section_starts = build_detail_messages(linked.sections, SLACK_MESSAGE_LIMIT)
        placeholder_urls = [placeholder] * len(linked.sections)
        summary_msgs = build_summary_messages(linked_replies, placeholder_urls, SLACK_MESSAGE_LIMIT, bullet_fn=self._bullet)

        n_summary = len(summary_msgs)
        all_reply_msgs = summary_msgs + detail_msgs

        # Phase 2: Handle the OP separately, then sync replies
        if thread_ts is None:
            # New thread: post OP with summary_prefix
            if not dry_run:
                op_content = linked.summary_prefix or " "
                op = self.post(op_content)
                thread_ts = op.id
            else:
                thread_ts = "<new>"
        elif linked.summary_prefix and not dry_run:
            # Existing thread: edit OP with summary_prefix
            self.edit(thread_ts, linked.summary_prefix)

        # Sync reply messages (skip OP in list_messages)
        self._skip_op = True
        try:
            result = self.sync(
                Thread(messages=all_reply_msgs),
                thread_ts=thread_ts,
                dry_run=dry_run,
                pace=pace,
                jitter=jitter,
                suppress_unfurls=suppress_unfurls,
            )
        finally:
            self._skip_op = False

        if dry_run:
            return LinkedSyncResult(
                thread_id=thread_ts,
                summary_ids=result.message_ids[:n_summary],
                detail_ids=result.message_ids[n_summary:],
                section_detail_ids={},
            )

        tid = result.thread_id
        detail_ids = result.message_ids[n_summary:]
        summary_ids = result.message_ids[:n_summary]

        # Phase 3: Resolve real permalinks and build links
        section_detail_map: dict[str, str] = {}
        real_links: list[str] = []
        for i, section in enumerate(linked.sections):
            if i > 0 and pace > 0:
                time.sleep(pace + random.uniform(0, jitter))
            detail_idx = section_starts[i]
            detail_msg_id = detail_ids[detail_idx]
            section_detail_map[section.title] = detail_msg_id
            real_links.append(self.permalink(detail_msg_id))

        # Phase 4: Rebuild summaries with real links and edit
        final_summaries = build_summary_messages(linked_replies, real_links, SLACK_MESSAGE_LIMIT, bullet_fn=self._bullet)
        for i, (msg_id, content) in enumerate(zip(summary_ids, final_summaries)):
            if i > 0 and pace > 0:
                time.sleep(pace + random.uniform(0, jitter))
            self.edit(msg_id, content)

        return LinkedSyncResult(
            thread_id=tid,
            summary_ids=summary_ids,
            detail_ids=detail_ids,
            section_detail_ids=section_detail_map,
        )
