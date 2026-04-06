from __future__ import annotations

import json
import subprocess

from .core import EditRateLimited, Message, SyncOptions, SyncResult, Thread, sync

DISCORD_API = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000


class DiscordClient:
    def __init__(self, token: str, channel_id: str):
        self.token = token if token.startswith("Bot ") else f"Bot {token}"
        self.channel_id = channel_id
        self._active_thread_id: str | None = None

    def _curl(
        self,
        method: str,
        path: str,
        data: dict | None = None,
    ) -> dict | None:
        url = f"{DISCORD_API}{path}"
        cmd = [
            "curl", "-s",
            "-X", method,
            "-H", f"Authorization: {self.token}",
            "-H", "Content-Type: application/json",
        ]
        if data is not None:
            cmd += ["-d", json.dumps(data)]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if not result.stdout.strip():
            return None
        resp = json.loads(result.stdout)
        if isinstance(resp, dict) and "code" in resp and "message" in resp:
            if resp["code"] == 30046:
                raise EditRateLimited(resp["message"])
            raise RuntimeError(f"Discord API error: {resp['message']} (code {resp['code']})")
        return resp

    @property
    def _channel(self) -> str:
        return self._active_thread_id or self.channel_id

    def list_messages(self, thread_id: str) -> list[Message]:
        # Discord returns messages newest-first; reverse to get chronological order
        resp = self._curl("GET", f"/channels/{thread_id}/messages?limit=100")
        if not resp:
            return []
        return [
            Message(id=m["id"], content=m.get("content", ""))
            for m in reversed(resp)
            if m.get("type", 0) == 0
        ]

    def post(self, content: str, thread_id: str | None = None) -> Message:
        if len(content) > MESSAGE_LIMIT:
            raise ValueError(f"Message exceeds Discord's {MESSAGE_LIMIT} char limit ({len(content)} chars)")
        channel = thread_id or self._channel
        data: dict = {"content": content}
        resp = self._curl("POST", f"/channels/{channel}/messages", data)
        return Message(id=resp["id"], content=content)

    def create_thread(self, message_id: str, name: str) -> str:
        """Create a thread from a message, return the thread channel ID."""
        resp = self._curl("POST", f"/channels/{self.channel_id}/messages/{message_id}/threads", {
            "name": name,
        })
        return resp["id"]

    def edit(self, message_id: str, content: str) -> Message:
        if len(content) > MESSAGE_LIMIT:
            raise ValueError(f"Message exceeds Discord's {MESSAGE_LIMIT} char limit ({len(content)} chars)")
        self._curl("PATCH", f"/channels/{self._channel}/messages/{message_id}", {
            "content": content,
        })
        return Message(id=message_id, content=content)

    def delete(self, message_id: str) -> None:
        self._curl("DELETE", f"/channels/{self._channel}/messages/{message_id}")

    def sync(
        self,
        thread: Thread,
        thread_id: str | None = None,
        dry_run: bool = False,
        suppress_embeds: bool = False,
        thread_name: str | None = None,
    ) -> SyncResult:
        self._active_thread_id = thread_id
        try:
            return sync(
                client=self,
                desired=thread,
                thread_id=thread_id,
                options=SyncOptions(
                    dry_run=dry_run,
                    suppress_embeds=suppress_embeds,
                    thread_name=thread_name,
                ),
            )
        finally:
            self._active_thread_id = None
