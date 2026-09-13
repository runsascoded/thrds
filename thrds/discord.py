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


class _DiscordHTTP:
    """Shared curl-based HTTP core for the two Discord transports.

    `DiscordClient` (bot token) and `DiscordWebhookClient` (webhook execution)
    are two identity models over one transport — same retry/backoff, 429 +
    `EditRateLimited` handling, and status parsing. Subclasses build the URL and
    auth headers; this holds the request loop.

    Error strings carry a caller-supplied ``label`` rather than the raw URL, so
    a webhook client (whose URL embeds a secret token) never leaks it.
    """

    _MAX_ATTEMPTS = 4
    _BACKOFF_BASE = 1.0
    _BACKOFF_CAP = 15.0
    _BODY_SNIPPET = 500

    def _backoff(self, attempt: int) -> float:
        return min(self._BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1.0), self._BACKOFF_CAP)

    def _curl_raw(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        label: str | None = None,
    ) -> dict | list | None:
        label = label or f"{method} {url}"
        last_err = ""
        for attempt in range(self._MAX_ATTEMPTS):
            cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method]
            for key, value in (headers or {}).items():
                cmd += ["-H", f"{key}: {value}"]
            if data is not None:
                cmd += ["-d", json.dumps(data)]
            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if result.returncode != 0:
                last_err = f"curl exit {result.returncode}: {result.stderr.strip()[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: {last_err}")

            body, _, status_str = result.stdout.rpartition("\n")
            try:
                status = int(status_str.strip())
            except ValueError:
                last_err = f"unparseable status line from curl: {result.stdout[:self._BODY_SNIPPET]!r}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: {last_err}")

            body = body.strip()

            if status == 204:
                return None

            if 500 <= status < 600:
                last_err = f"HTTP {status}: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: {last_err}")

            try:
                resp = json.loads(body) if body else None
            except json.JSONDecodeError:
                last_err = f"HTTP {status}, non-JSON body: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: {last_err}")

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
                    raise RuntimeError(f"Discord {label}: rate-limited after {attempt+1} attempts: {last_err}")
                raise RuntimeError(f"Discord API error: {resp['message']} (code {resp['code']})")

            if status == 429:
                last_err = f"HTTP 429: {body[:self._BODY_SNIPPET]}"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: rate-limited after {attempt+1} attempts: {last_err}")

            if 200 <= status < 300 and resp is None:
                last_err = f"HTTP {status}, empty body (expected entity)"
                if attempt + 1 < self._MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"Discord {label}: {last_err}")

            if 400 <= status < 500:
                raise RuntimeError(f"Discord {label}: HTTP {status}: {body[:self._BODY_SNIPPET]}")

            return resp

        raise RuntimeError(f"Discord {label}: retries exhausted: {last_err}")


class DiscordClient(_DiscordHTTP):
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
    ) -> dict | list | None:
        return self._curl_raw(
            method,
            f"{DISCORD_API}{path}",
            data,
            headers={"Authorization": self.token, "Content-Type": "application/json"},
            label=f"{method} {path}",
        )

    @property
    def _channel(self) -> str:
        return self._active_thread_id or self.channel_id

    def _channel_for(self, message_id: str) -> str:
        """Channel id to address ``message_id`` for edit/delete.

        A thrds thread's OP lives in the **parent** channel but shares the
        thread's id (``create_thread`` returns a channel whose id equals the
        starter message's id). So during a thread reconcile — ``_active_thread_id``
        set to the thread — the one message whose id equals that thread id is
        the OP, and must be addressed via the parent channel:
        ``PATCH /channels/{thread}/messages/{op}`` is a ``10008 Unknown Message``.
        Every other message is a real thread reply, addressed via the thread.
        """
        if self._active_thread_id is not None and message_id == self._active_thread_id:
            return self.channel_id
        return self._channel

    def list_messages(self, thread_id: str) -> list[Message]:
        # Discord returns messages newest-first; reverse to get chronological order
        resp = self._curl("GET", f"/channels/{thread_id}/messages?limit=100")
        messages = [
            Message(id=m["id"], content=m.get("content", ""))
            for m in reversed(resp or [])
            if m.get("type", 0) == 0
        ]
        # A thread created off a message shares that message's id, and the OP
        # itself lives in the PARENT channel — `GET /channels/{thread}/messages`
        # returns only the replies (plus an empty type-21 starter placeholder,
        # already dropped by the type-0 filter above), never the OP. Prepend it
        # so `sync`'s positional diff aligns (existing[0] is the OP). The OP's
        # id is the thread id. Listing the base channel itself (thread_id ==
        # channel_id, e.g. a lone-OP reconcile scoped by `only_ids`) has no such
        # separate OP, so this only fires for an actual child thread.
        if thread_id != self.channel_id:
            op = self._curl("GET", f"/channels/{self.channel_id}/messages/{thread_id}")
            if isinstance(op, dict) and op.get("type", 0) == 0:
                messages.insert(0, Message(id=op["id"], content=op.get("content", "")))
        return messages

    def post(
        self,
        content: str,
        thread_id: str | None = None,
        *,
        username: str | None = None,
        icon_url: str | None = None,
        icon_emoji: str | None = None,
    ) -> Message:
        """Post a message as the bot.

        The per-message sender kwargs (``username``/``icon_url``/``icon_emoji``)
        exist for `ThreadClient` protocol parity, but Discord's **bot** API
        cannot set a per-message sender — so any non-None value **raises**
        rather than being silently dropped. Per-message identity on Discord
        requires the webhook transport (a webhook-backed client; see
        ``specs/discord-push.md``); route such threads there.
        """
        if username is not None or icon_url is not None or icon_emoji is not None:
            raise NotImplementedError(
                "Discord's bot API cannot set a per-message sender "
                "(username/icon_url/icon_emoji); post via a webhook-backed "
                "client for per-message identity. See specs/discord-push.md."
            )
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

    def open_thread(self, op_id: str, name: str | None) -> str:
        """Open a child thread off message ``op_id``; return its channel id.

        The `_reply_target` seam in `core.sync` calls this so a fresh push posts
        the OP to the channel and its replies into a real Discord thread — a
        message id is not a channel, so replies can't address the OP directly
        the way Slack's ``thread_ts`` does. Discord requires a thread name
        (1–100 chars); the caller must supply one.
        """
        if not name:
            raise ValueError(
                "Discord requires a thread name to open a thread; pass "
                "`thread_name` (e.g. `--thread-name`, or `SyncOptions.thread_name`)."
            )
        return self.create_thread(op_id, name)

    def edit(self, message_id: str, content: str) -> Message:
        if len(content) > MESSAGE_LIMIT:
            raise ValueError(f"Message exceeds Discord's {MESSAGE_LIMIT} char limit ({len(content)} chars)")
        data: dict = {"content": content}
        if self._suppress_embeds:
            data["flags"] = 4
        self._curl("PATCH", f"/channels/{self._channel_for(message_id)}/messages/{message_id}", data)
        return Message(id=message_id, content=content)

    def delete(self, message_id: str) -> None:
        self._curl("DELETE", f"/channels/{self._channel_for(message_id)}/messages/{message_id}")

    def sync(
        self,
        thread: Thread,
        thread_id: str | None = None,
        dry_run: bool = False,
        pace: float = 0.0,
        jitter: float = 0.0,
        suppress_embeds: bool = False,
        thread_name: str | None = None,
        only_ids: set[str] | None = None,
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
                    only_ids=only_ids,
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


class DiscordWebhookClient(_DiscordHTTP):
    """Per-message-sender Discord transport via webhook execution.

    A webhook sets ``username`` + ``avatar_url`` **per message** — the bot API
    can't, so `DiscordClient.post` raises on sender overrides and points here.
    But a webhook can't open a thread and can't list/read messages, so this
    client is deliberately write-only (``post`` / ``edit`` / ``delete``) and
    pairs with a `DiscordClient` that owns thread creation, listing, and
    reconcile: the bot opens the thread and owns the OP; this client fans
    per-sender replies into it via ``?thread_id=``. See specs/discord-push.md.

    The protocol's ``icon_url`` maps to the webhook's ``avatar_url`` (both a
    hosted image URL); ``icon_emoji`` has no webhook equivalent and raises.
    Per-message values override the client-wide ``username`` / ``avatar_url``
    defaults (mirroring `SlackClient.post`).

    ``webhook_url`` embeds the webhook's secret token, so it is never logged —
    error labels carry only the method and message id, never the URL.
    """

    _HEADERS = {"Content-Type": "application/json"}

    def __init__(
        self,
        webhook_url: str,
        thread_id: str | None = None,
        *,
        username: str | None = None,
        avatar_url: str | None = None,
        suppress_embeds: bool = False,
    ):
        self.webhook_url = webhook_url
        self.thread_id = thread_id
        self.username = username
        self.avatar_url = avatar_url
        self.suppress_embeds = suppress_embeds

    def _message_url(self, message_id: str) -> str:
        url = f"{self.webhook_url}/messages/{message_id}"
        if self.thread_id is not None:
            url += f"?thread_id={self.thread_id}"
        return url

    def list_messages(self, thread_id: str) -> list[Message]:
        raise NotImplementedError(
            "A webhook can't read messages. Use a `DiscordClient` (bot token) to "
            "list/reconcile the thread, and this client only to post/edit/delete "
            "per-sender replies. See specs/discord-push.md."
        )

    def post(
        self,
        content: str,
        thread_id: str | None = None,
        *,
        username: str | None = None,
        icon_url: str | None = None,
        icon_emoji: str | None = None,
    ) -> Message:
        if icon_emoji is not None:
            raise NotImplementedError(
                "Discord webhooks have no emoji avatar; pass a hosted image URL as "
                "`icon_url` (mapped to the webhook's `avatar_url`)."
            )
        if len(content) > MESSAGE_LIMIT:
            raise ValueError(f"Message exceeds Discord's {MESSAGE_LIMIT} char limit ({len(content)} chars)")
        tid = thread_id if thread_id is not None else self.thread_id
        url = f"{self.webhook_url}?wait=true"
        if tid is not None:
            url += f"&thread_id={tid}"
        data: dict = {"content": content}
        resolved_username = username if username is not None else self.username
        if resolved_username is not None:
            data["username"] = resolved_username
        resolved_avatar = icon_url if icon_url is not None else self.avatar_url
        if resolved_avatar is not None:
            data["avatar_url"] = resolved_avatar
        if self.suppress_embeds:
            data["flags"] = 4
        resp = self._curl_raw("POST", url, data, headers=self._HEADERS, label="POST webhook message")
        return Message(id=resp["id"], content=content)

    def edit(self, message_id: str, content: str) -> Message:
        if len(content) > MESSAGE_LIMIT:
            raise ValueError(f"Message exceeds Discord's {MESSAGE_LIMIT} char limit ({len(content)} chars)")
        data: dict = {"content": content}
        if self.suppress_embeds:
            data["flags"] = 4
        self._curl_raw(
            "PATCH", self._message_url(message_id), data,
            headers=self._HEADERS, label=f"PATCH webhook message {message_id}",
        )
        return Message(id=message_id, content=content)

    def delete(self, message_id: str) -> None:
        self._curl_raw(
            "DELETE", self._message_url(message_id),
            headers=self._HEADERS, label=f"DELETE webhook message {message_id}",
        )
