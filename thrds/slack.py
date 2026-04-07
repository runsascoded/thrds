from __future__ import annotations

import json
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode

from .core import Message, SyncOptions, SyncResult, Thread, sync


class SlackClient:
    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel
        self._suppress_unfurls: bool = True
        self._metadata_by_content: dict[str, dict] | None = None

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
        return [
            Message(id=m["ts"], content=m.get("text", ""))
            for m in result.get("messages", [])
        ]

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
                    suppress_unfurls=suppress_unfurls,
                ),
            )
        finally:
            self._metadata_by_content = None
