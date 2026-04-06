from __future__ import annotations

import json
import urllib.request
from urllib.error import HTTPError

from .core import Message, SyncOptions, SyncResult, Thread, sync


class SlackClient:
    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel

    def _request(
        self,
        method: str,
        data: dict | None = None,
    ) -> dict:
        url = f"https://slack.com/api/{method}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(f"Slack API error: {e.code} {e.read().decode()}") from e
        if not result.get("ok"):
            raise RuntimeError(f"Slack API error: {result.get('error', result)}")
        return result

    def list_messages(self, thread_id: str) -> list[Message]:
        result = self._request("conversations.replies", {
            "channel": self.channel,
            "ts": thread_id,
        })
        return [
            Message(id=m["ts"], content=m.get("text", ""))
            for m in result.get("messages", [])
        ]

    def post(self, content: str, thread_id: str | None = None) -> Message:
        data: dict = {
            "channel": self.channel,
            "text": content,
        }
        if thread_id is not None:
            data["thread_ts"] = thread_id
        result = self._request("chat.postMessage", data)
        return Message(id=result["ts"], content=content)

    def edit(self, message_id: str, content: str) -> Message:
        self._request("chat.update", {
            "channel": self.channel,
            "ts": message_id,
            "text": content,
        })
        return Message(id=message_id, content=content)

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
    ) -> SyncResult:
        return sync(
            client=self,
            desired=thread,
            thread_id=thread_ts,
            options=SyncOptions(dry_run=dry_run),
        )
