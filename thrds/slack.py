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
        result = self._request("chat.postMessage", data)
        return Message(id=result["ts"], content=content)

    def edit(self, message_id: str, content: str) -> Message:
        self._request("chat.update", {
            "channel": self.channel,
            "ts": message_id,
            "text": content,
            "unfurl_links": not self._suppress_unfurls,
            "unfurl_media": not self._suppress_unfurls,
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
        pace: float = 0.4,
        suppress_unfurls: bool = True,
    ) -> SyncResult:
        self._suppress_unfurls = suppress_unfurls
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
