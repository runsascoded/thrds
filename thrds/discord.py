from __future__ import annotations

import json
import random
import subprocess
import time

from .core import EditRateLimited, Message, SyncOptions, SyncResult, Thread, sync
from .linked import LinkedSyncResult, LinkedThread, build_detail_messages, build_summary_messages

DISCORD_API = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000


class DiscordClient:
    def __init__(self, token: str, channel_id: str, guild_id: str | None = None):
        self.token = token if token.startswith("Bot ") else f"Bot {token}"
        self.channel_id = channel_id
        self.guild_id = guild_id
        self._active_thread_id: str | None = None
        self._suppress_embeds: bool = False

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
        if self._suppress_embeds:
            data["flags"] = 4
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
        data: dict = {"content": content}
        if self._suppress_embeds:
            data["flags"] = 4
        self._curl("PATCH", f"/channels/{self._channel}/messages/{message_id}", data)
        return Message(id=message_id, content=content)

    def delete(self, message_id: str) -> None:
        self._curl("DELETE", f"/channels/{self._channel}/messages/{message_id}")

    def sync(
        self,
        thread: Thread,
        thread_id: str | None = None,
        dry_run: bool = False,
        pace: float = 0.0,
        jitter: float = 0.0,
        suppress_embeds: bool = False,
        thread_name: str | None = None,
    ) -> SyncResult:
        self._active_thread_id = thread_id
        self._suppress_embeds = suppress_embeds
        try:
            return sync(
                client=self,
                desired=thread,
                thread_id=thread_id,
                options=SyncOptions(
                    dry_run=dry_run,
                    pace=pace,
                    jitter=jitter,
                    suppress_embeds=suppress_embeds,
                    thread_name=thread_name,
                ),
            )
        finally:
            self._active_thread_id = None
            self._suppress_embeds = False

    def _detail_url(self, message_id: str, thread_id: str) -> str:
        """Build a Discord message URL."""
        return f"https://discord.com/channels/{self.guild_id}/{thread_id}/{message_id}"

    def _detail_url_placeholder(self) -> str:
        """Placeholder URL with max possible length for space reservation."""
        # Discord snowflake IDs are up to 20 digits
        fake_id = "0" * 20
        return f"https://discord.com/channels/{self.guild_id}/{fake_id}/{fake_id}"

    def sync_linked(
        self,
        linked: LinkedThread,
        thread_id: str | None = None,
        dry_run: bool = False,
        pace: float = 0.0,
        jitter: float = 0.0,
        suppress_embeds: bool = False,
    ) -> LinkedSyncResult:
        """Sync a linked summary thread."""
        if not self.guild_id:
            raise ValueError("`guild_id` required for `sync_linked` (needed for message links)")

        placeholder = self._detail_url_placeholder()

        # Phase 1: Build detail + summary messages with placeholder links
        detail_msgs, section_starts = build_detail_messages(linked.sections, MESSAGE_LIMIT)
        placeholder_urls = [placeholder] * len(linked.sections)
        summary_msgs = build_summary_messages(linked, placeholder_urls, MESSAGE_LIMIT)

        n_summary = len(summary_msgs)
        all_msgs = summary_msgs + detail_msgs

        # Phase 2: Sync all messages
        result = self.sync(
            Thread(messages=all_msgs),
            thread_id=thread_id,
            dry_run=dry_run,
            pace=pace,
            jitter=jitter,
            suppress_embeds=suppress_embeds,
        )

        if dry_run:
            return LinkedSyncResult(
                thread_id=result.thread_id,
                summary_ids=result.message_ids[:n_summary],
                detail_ids=result.message_ids[n_summary:],
                section_detail_ids={},
            )

        tid = result.thread_id
        detail_ids = result.message_ids[n_summary:]
        summary_ids = result.message_ids[:n_summary]

        # Phase 3: Build section → detail ID map and real links
        section_detail_map: dict[str, str] = {}
        real_links: list[str] = []
        for i, section in enumerate(linked.sections):
            detail_idx = section_starts[i]
            detail_msg_id = detail_ids[detail_idx]
            section_detail_map[section.title] = detail_msg_id
            real_links.append(self._detail_url(detail_msg_id, tid))

        # Phase 4: Rebuild summaries with real links and edit
        # Set _active_thread_id so edits target the thread, not the parent channel
        self._active_thread_id = tid
        self._suppress_embeds = suppress_embeds
        try:
            final_summaries = build_summary_messages(linked, real_links, MESSAGE_LIMIT)
            for i, (msg_id, content) in enumerate(zip(summary_ids, final_summaries)):
                if i > 0 and pace > 0:
                    time.sleep(pace + random.uniform(0, jitter))
                self.edit(msg_id, content)
        finally:
            self._active_thread_id = None
            self._suppress_embeds = False

        return LinkedSyncResult(
            thread_id=tid,
            summary_ids=summary_ids,
            detail_ids=detail_ids,
            section_detail_ids=section_detail_map,
        )
