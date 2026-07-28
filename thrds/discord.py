from __future__ import annotations

import json
import random
import subprocess
import time

from .core import EditRateLimited, Message, SyncOptions, SyncResult, Thread, sync
from .linked import (
    LinkedSyncResult,
    LinkedThread,
    build_detail_messages,
    build_summary_partition,
    render_summary_from_partition,
)

DISCORD_API = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000


class DiscordClient:
    def __init__(self, token: str, channel_id: str, guild_id: str | None = None):
        self.token = token if token.startswith("Bot ") else f"Bot {token}"
        self.channel_id = channel_id
        self.guild_id = guild_id
        self._active_thread_id: str | None = None
        self._suppress_embeds: bool = False

    _MAX_ATTEMPTS = 4
    _BACKOFF_BASE = 1.0
    _BACKOFF_CAP = 15.0
    _BODY_SNIPPET = 500

    def _backoff(self, attempt: int) -> float:
        return min(self._BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1.0), self._BACKOFF_CAP)

    def _curl(
        self,
        method: str,
        path: str,
        data: dict | None = None,
    ) -> dict | list | None:
        url = f"{DISCORD_API}{path}"
        last_err = ""
        for attempt in range(self._MAX_ATTEMPTS):
            cmd = [
                "curl", "-s",
                "-w", "\n%{http_code}",
                "-X", method,
                "-H", f"Authorization: {self.token}",
                "-H", "Content-Type: application/json",
            ]
            if data is not None:
                cmd += ["-d", json.dumps(data)]
            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if result.returncode != 0:
                last_err = f"curl exit {result.returncode}: {result.stderr.strip()[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: {last_err}")

            body, _, status_str = result.stdout.rpartition("\n")
            try:
                status = int(status_str.strip())
            except ValueError:
                last_err = f"unparseable status line from curl: {result.stdout[:self._BODY_SNIPPET]!r}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: {last_err}")

            body = body.strip()

            if status == 204:
                return None

            if 500 <= status < 600:
                last_err = f"HTTP {status}: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: {last_err}")

            try:
                resp = json.loads(body) if body else None
            except json.JSONDecodeError:
                last_err = f"HTTP {status}, non-JSON body: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: {last_err}")

            # Structured Discord error dict — handle before generic 4xx so
            # `EditRateLimited` (code 30046, HTTP 429) is preserved and other
            # 4xx errors surface Discord's message.
            if isinstance(resp, dict) and "code" in resp and "message" in resp:
                if resp["code"] == 30046:
                    raise EditRateLimited(resp["message"])
                if status == 429:
                    retry_after = resp.get("retry_after")
                    delay = float(retry_after) if isinstance(retry_after, (int, float)) else self._backoff(attempt)
                    last_err = f"HTTP 429: {body[:self._BODY_SNIPPET]}"
                    if attempt + 1 < self._MAX_ATTEMPTS:
                        time.sleep(min(delay, self._BACKOFF_CAP))
                        continue
                    raise RuntimeError(f"Discord {method} {path}: rate-limited after {attempt+1} attempts: {last_err}")
                raise RuntimeError(f"Discord API error: {resp['message']} (code {resp['code']})")

            if status == 429:
                last_err = f"HTTP 429: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: rate-limited after {attempt+1} attempts: {last_err}")

            if 200 <= status < 300 and resp is None:
                last_err = f"HTTP {status}, empty body (expected entity)"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {method} {path}: {last_err}")

            if 400 <= status < 500:
                raise RuntimeError(f"Discord {method} {path}: HTTP {status}: {body[:self._BODY_SNIPPET]}")

            return resp

        raise RuntimeError(f"Discord {method} {path}: retries exhausted: {last_err}")

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
        summary_msgs, partition = build_summary_partition(linked, placeholder_urls, MESSAGE_LIMIT)

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
            # Rebuild each phase-1 message with real URLs, preserving the
            # partition (see `SlackClient.sync_linked` for rationale).
            final_summaries = render_summary_from_partition(linked, real_links, partition)
            if len(final_summaries) != len(summary_ids):
                raise RuntimeError(
                    f"sync_linked phase-4 render yielded {len(final_summaries)} summary messages, "
                    f"phase-1 posted {len(summary_ids)}; partition invariant violated."
                )
            for j, msg in enumerate(final_summaries):
                if len(msg) > MESSAGE_LIMIT:
                    raise RuntimeError(
                        f"sync_linked phase-4 message {j} rendered to {len(msg)} chars "
                        f"(limit {MESSAGE_LIMIT}); real message URLs longer than "
                        "the placeholder upper bound."
                    )
            for i, (msg_id, content) in enumerate(zip(summary_ids, final_summaries, strict=True)):
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
