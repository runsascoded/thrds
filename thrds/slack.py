from __future__ import annotations

import json
import random
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode

from .core import Message, OrphanedRepliesError, SyncOptions, SyncResult, Thread, sync
from .doc import Doc, DocMessage, DocSyncResult, DocThread
from .linked import (
    LinkedSyncResult,
    LinkedThread,
    Section,
    build_detail_messages,
    build_summary_partition,
    render_summary_from_partition,
)
from .state import SessionState

THRDS_METADATA_EVENT_TYPE = 'thrds'

SLACK_MESSAGE_LIMIT = 4000


class SlackClient:
    def __init__(
        self,
        token: str,
        channel: str,
        username: str | None = None,
        icon_emoji: str | None = None,
    ):
        self.token = token
        self.channel = channel
        self.username = username
        self.icon_emoji = icon_emoji
        self._suppress_unfurls: bool = True
        self._metadata_by_content: dict[str, dict] | None = None
        self._skip_op: bool = False
        self._bot_ids: tuple[str, str | None] | None = None
        self._user_name_cache: dict[str, str] = {}

    @property
    def bot_ids(self) -> tuple[str, str | None]:
        """Lazily resolve and cache the authenticated bot's (user_id, bot_id).

        Both are needed to tag messages as `editable`: Slack returns `user`
        on human messages (match via user_id) but `bot_id` with `user: null`
        on bot_message events, so our own posts would be marked non-editable
        if we only checked `user`.
        """
        if self._bot_ids is None:
            result = self._request("auth.test", method="POST")
            self._bot_ids = (result["user_id"], result.get("bot_id"))
        return self._bot_ids

    @property
    def bot_user_id(self) -> str:
        """Back-compat alias. Prefer `bot_ids` for bot/user_id both."""
        return self.bot_ids[0]

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
        user_id, bot_id = self.bot_ids
        messages = [
            Message(
                id=m["ts"],
                content=m.get("text", ""),
                # Slack bot_messages come back with `user: null` and `bot_id`
                # set; human messages carry `user`. Match either so our own
                # bot's posts are correctly marked editable.
                editable=(
                    m.get("user") == user_id
                    or (bot_id is not None and m.get("bot_id") == bot_id)
                ),
            )
            for m in result.get("messages", [])
        ]
        # In sync_linked mode, skip the OP (thread parent) — it's managed separately
        if self._skip_op and messages:
            messages = messages[1:]
        return messages

    def post(self, content: str, thread_id: str | None = None) -> Message:
        if len(content) > SLACK_MESSAGE_LIMIT:
            raise ValueError(
                f"Message exceeds Slack's {SLACK_MESSAGE_LIMIT} char limit ({len(content)} chars)"
            )
        data: dict = {
            "channel": self.channel,
            "text": content,
            "unfurl_links": not self._suppress_unfurls,
            "unfurl_media": not self._suppress_unfurls,
        }
        if self.username is not None:
            data["username"] = self.username
        if self.icon_emoji is not None:
            data["icon_emoji"] = self.icon_emoji
        if thread_id is not None:
            data["thread_ts"] = thread_id
        md = self._metadata_for(content)
        if md is not None:
            data["metadata"] = md
        result = self._request("chat.postMessage", data)
        return Message(id=result["ts"], content=content)

    def edit(self, message_id: str, content: str) -> Message:
        if len(content) > SLACK_MESSAGE_LIMIT:
            raise ValueError(
                f"Message exceeds Slack's {SLACK_MESSAGE_LIMIT} char limit ({len(content)} chars)"
            )
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

    def delete(self, message_id: str, orphans_ok: bool = False) -> None:
        if not orphans_ok:
            result = self._request("conversations.replies", {
                "channel": self.channel,
                "ts": message_id,
            }, method="GET")
            replies = result.get("messages", [])
            if len(replies) > 1:
                raise OrphanedRepliesError(message_id, len(replies) - 1)
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
        """Placeholder URL sized to bound real Slack permalink lengths.

        Real permalinks (workspace + channel + thread + cid params) typically
        run ~110-140 chars; 180 is a safe upper bound so phase-4 real-URL
        packing produces at most the phase-1 message count. Any residual
        mismatch is caught by the ``strict=True`` zip in phase 4.
        """
        return "x" * 180

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
        summary_msgs, partition = build_summary_partition(linked_replies, placeholder_urls, SLACK_MESSAGE_LIMIT, bullet_fn=self._bullet)

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

        # Phase 4: Rebuild each phase-1 message with real URLs, preserving the
        # partition — this keeps the message count constant regardless of real
        # vs placeholder URL length, so the strict-zip below is an invariant
        # assertion (unreachable absent a bug in `build_summary_partition`),
        # not a common-case failure mode.
        final_summaries = render_summary_from_partition(linked_replies, real_links, partition, bullet_fn=self._bullet)
        if len(final_summaries) != len(summary_ids):
            raise RuntimeError(
                f"sync_linked phase-4 render yielded {len(final_summaries)} summary messages, "
                f"phase-1 posted {len(summary_ids)}; partition invariant violated."
            )
        for j, msg in enumerate(final_summaries):
            if len(msg) > SLACK_MESSAGE_LIMIT:
                raise RuntimeError(
                    f"sync_linked phase-4 message {j} rendered to {len(msg)} chars "
                    f"(limit {SLACK_MESSAGE_LIMIT}); real permalinks longer than "
                    "`_detail_url_placeholder` upper bound — bump the placeholder."
                )
        for i, (msg_id, content) in enumerate(zip(summary_ids, final_summaries, strict=True)):
            if i > 0 and pace > 0:
                time.sleep(pace + random.uniform(0, jitter))
            self.edit(msg_id, content)

        return LinkedSyncResult(
            thread_id=tid,
            summary_ids=summary_ids,
            detail_ids=detail_ids,
            section_detail_ids=section_detail_map,
        )

    # --- Doc-level sync (multi-thread) ---
    def create_private_channel(self, name: str) -> str:
        """Create a private channel with ``name`` and return its channel_id.

        The Slack API may lowercase / rewrite invalid chars in the name; the
        returned ID (not the name) is the durable handle to record in state.
        Requires the token to have ``groups:write``.
        """
        result = self._request("conversations.create", {
            "name": name,
            "is_private": True,
        })
        return result["channel"]["id"]

    def archive_channel(self, channel: str) -> None:
        """Archive ``channel`` (reversible via ``conversations.unarchive``).

        The primary use is GC of a session's staging PC after a successful
        prod push; Slack has no channel-delete for standard workspaces, so
        archive is the strongest cleanup available.
        """
        self._request("conversations.archive", {"channel": channel})

    def _delete_thread(self, thread_ts: str, pace: float, jitter: float) -> None:
        """Delete an entire thread — replies bottom-up, OP last.

        Foreign replies (non-editable) will fail chat.delete; we skip them via
        ``editable`` filter. That leaves them orphaned if any exist, but in a
        single-member staging PC that shouldn't happen in practice.
        """
        replies = self.list_messages(thread_ts)
        # replies[0] is the OP itself; skip and delete OP last.
        for msg in reversed(replies[1:]):
            if not msg.editable:
                continue
            self._request("chat.delete", {"channel": self.channel, "ts": msg.id})
            if pace > 0:
                time.sleep(pace + random.uniform(0, jitter))
        # OP:
        self._request("chat.delete", {"channel": self.channel, "ts": thread_ts})

    def _thrds_metadata(
        self,
        state: SessionState,
        thread_slug: str | None,
        kind: str,
    ) -> dict:
        """Build the Slack metadata payload for one thrds-owned message.

        Emitted on every post/edit so ``recover`` can rebuild state from
        ``conversations.history`` by filtering on ``event_type == 'thrds'``
        and ``event_payload.session_id == <id>``.
        """
        payload = {
            "session_id": state.session_id,
            "doc_slug": state.doc_slug,
            "kind": kind,
        }
        if thread_slug is not None:
            payload["thread_slug"] = thread_slug
        return {
            "event_type": THRDS_METADATA_EVENT_TYPE,
            "event_payload": payload,
        }

    def _sync_doc_thread(
        self,
        thread: DocThread,
        existing_ts: str | None,
        state: SessionState,
        dry_run: bool,
        pace: float,
        jitter: float,
        suppress_unfurls: bool,
    ) -> tuple[str, SyncResult]:
        """Sync one owned thread (its OP + replies) via `sync()`.

        Filters ``thread.messages`` to ours-only (``author is None``) before
        translating to a `Thread` — foreign messages are preserved on Slack
        by `core.sync`'s editable filter, never re-posted. Returns
        ``(op_ts, sync_result)``.
        """
        ours = [m for m in thread.messages if m.author is None]
        if not ours:
            raise ValueError(
                f"Thread {thread.slug!r}: no ours messages to sync — every "
                "message has an author, which shouldn't be possible (OP must "
                "be ours). Data-model bug."
            )
        core_thread = Thread(messages=[m.content for m in ours])
        metadata: dict[str, dict] = {}
        for i, m in enumerate(ours):
            kind = "op" if i == 0 else "reply"
            metadata[m.content] = self._thrds_metadata(state, thread.slug, kind)
        result = self.sync(
            core_thread,
            thread_ts=existing_ts,
            dry_run=dry_run,
            pace=pace,
            jitter=jitter,
            suppress_unfurls=suppress_unfurls,
            metadata=metadata,
        )
        return result.thread_id, result

    def _sync_preamble(
        self,
        preamble: str | None,
        existing_ts: str | None,
        state: SessionState,
        dry_run: bool,
        pace: float,
        jitter: float,
        suppress_unfurls: bool,
        delete_on_absent: bool,
    ) -> str | None:
        """Sync the doc's preamble (a top-level message with no replies).

        - preamble absent + no existing_ts → nop, return None.
        - preamble absent + existing_ts + delete_on_absent → delete, return None.
          (Staging terraforms; prod leaves the existing preamble in place.)
        - preamble present + no existing_ts → post fresh, return new ts.
        - preamble present + existing_ts → edit in place, return existing_ts.
        """
        if preamble is None:
            if existing_ts is not None and delete_on_absent and not dry_run:
                self._request("chat.delete", {"channel": self.channel, "ts": existing_ts})
                return None
            return existing_ts  # preserved as-is (or None if never set)
        # Treat the preamble as a one-message "thread" (no replies).
        core_thread = Thread(messages=[preamble])
        metadata = {preamble: self._thrds_metadata(state, thread_slug=None, kind="preamble")}
        result = self.sync(
            core_thread,
            thread_ts=existing_ts,
            dry_run=dry_run,
            pace=pace,
            jitter=jitter,
            suppress_unfurls=suppress_unfurls,
            metadata=metadata,
        )
        return result.thread_id if not dry_run else (existing_ts or "<new>")

    def sync_doc_staging(
        self,
        doc: Doc,
        state: SessionState,
        dry_run: bool = False,
        pace: float = 0.4,
        jitter: float = 0.0,
        suppress_unfurls: bool = True,
    ) -> DocSyncResult:
        """Terraform-sync a `Doc` into its per-session staging PC.

        Creates the PC lazily on the first non-dry-run call (updates
        ``state.staging_channel`` and persists state immediately so a mid-run
        failure doesn't leak the channel handle). Terraform semantics:

        - Slugged threads present in state but absent from the doc are deleted.
        - Slugged threads in the doc are synced in place (`core.sync` reconciles
          OP + replies, preserving foreign messages via the editable filter).
        - Preamble is treated as a one-message thread; deletion on removal.

        Every thread must have a slug (state is slug-keyed). Bare ``===``
        threads raise ``ValueError``.
        """
        for t in doc.threads:
            if t.slug is None:
                raise ValueError(
                    "sync_doc_staging requires every thread to have a slug; "
                    "bare `===` threads aren't state-trackable."
                )

        # 1. Ensure staging PC exists.
        if state.staging_channel is None and not dry_run:
            state.staging_channel = self.create_private_channel(state.staging_channel_name())
            state.save()  # persist eagerly — a mid-run failure shouldn't leak the channel handle
        channel = state.staging_channel if state.staging_channel is not None else "<new-pc>"

        prev_channel = self.channel
        self.channel = channel
        try:
            # 2. Preamble.
            preamble_ts = self._sync_preamble(
                doc.preamble,
                state.staging_preamble_ts,
                state,
                dry_run=dry_run,
                pace=pace,
                jitter=jitter,
                suppress_unfurls=suppress_unfurls,
                delete_on_absent=True,
            )
            if not dry_run:
                state.staging_preamble_ts = preamble_ts

            # 3. Delete stale slugs (in state, absent from doc).
            desired_slugs = {t.slug for t in doc.threads}
            stale_slugs = [s for s in list(state.staging_threads) if s not in desired_slugs]
            for slug in stale_slugs:
                thread_ts = state.staging_threads[slug]
                if not dry_run:
                    del state.staging_threads[slug]
                    self._delete_thread(thread_ts, pace=pace, jitter=jitter)

            # 4. Sync each desired thread.
            thread_ts_by_slug: dict[str, str] = {}
            thread_results: dict[str, SyncResult] = {}
            for thread in doc.threads:
                existing_ts = state.staging_threads.get(thread.slug)
                op_ts, sync_result = self._sync_doc_thread(
                    thread,
                    existing_ts,
                    state,
                    dry_run=dry_run,
                    pace=pace,
                    jitter=jitter,
                    suppress_unfurls=suppress_unfurls,
                )
                if not dry_run:
                    state.staging_threads[thread.slug] = op_ts
                thread_ts_by_slug[thread.slug] = op_ts
                thread_results[thread.slug] = sync_result

            if not dry_run:
                state.save()

            return DocSyncResult(
                channel=channel,
                preamble_ts=preamble_ts,
                thread_ts_by_slug=thread_ts_by_slug,
                thread_results=thread_results,
                deleted_slugs=stale_slugs,
            )
        finally:
            self.channel = prev_channel

    def sync_doc_prod(
        self,
        doc: Doc,
        state: SessionState,
        channel: str | None = None,
        keep_staging: bool = False,
        dry_run: bool = False,
        pace: float = 0.4,
        jitter: float = 0.0,
        suppress_unfurls: bool = True,
    ) -> DocSyncResult:
        """Additive sync of a `Doc` into its real target channel.

        Additive semantics (opposite of ``sync_doc_staging``): threads absent
        from the doc are LEFT in place on Slack (never terraform-deleted); the
        preamble on a channel is edited if the doc still has one but preserved
        otherwise. Owned threads present in state are reconciled by
        ``core.sync``; new threads in the doc get posted fresh and recorded in
        ``state.prod_threads[channel]``.

        ``channel`` overrides ``state.prod_channel`` for this call; the first
        successful (non-dry-run) call pins the resolved channel into
        ``state.prod_channel`` for subsequent runs.

        On success (non-dry-run), archives the session's staging PC unless
        ``keep_staging=True`` — the "prod push finalizes the session" GC step.
        The state records (``staging_channel`` + ``staging_threads``) are
        preserved as history; ``conversations.unarchive`` reopens the PC if
        needed.
        """
        for t in doc.threads:
            if t.slug is None:
                raise ValueError(
                    "sync_doc_prod requires every thread to have a slug; "
                    "bare `===` threads aren't state-trackable."
                )

        target = channel if channel is not None else state.prod_channel
        if target is None:
            raise ValueError(
                "No prod channel — pass channel= or set state.prod_channel first."
            )

        prev_channel = self.channel
        self.channel = target
        try:
            # 1. Preamble (additive: preserved on absence).
            preamble_ts = self._sync_preamble(
                doc.preamble,
                state.prod_preamble_ts.get(target),
                state,
                dry_run=dry_run,
                pace=pace,
                jitter=jitter,
                suppress_unfurls=suppress_unfurls,
                delete_on_absent=False,
            )
            if not dry_run:
                if preamble_ts is not None:
                    state.prod_preamble_ts[target] = preamble_ts
                # If preamble_ts is None here (no doc preamble, no prior state), leave state clean.

            # 2. Threads: sync-in-place, add-if-new. No delete of state entries absent from doc.
            thread_ts_by_slug: dict[str, str] = {}
            thread_results: dict[str, SyncResult] = {}
            for thread in doc.threads:
                existing_ts = state.get_thread_ts(target, thread.slug)
                op_ts, sync_result = self._sync_doc_thread(
                    thread,
                    existing_ts,
                    state,
                    dry_run=dry_run,
                    pace=pace,
                    jitter=jitter,
                    suppress_unfurls=suppress_unfurls,
                )
                if not dry_run:
                    state.set_thread_ts(target, thread.slug, op_ts)
                thread_ts_by_slug[thread.slug] = op_ts
                thread_results[thread.slug] = sync_result

            if not dry_run:
                state.prod_channel = target  # pin for future runs
                state.save()

            # 3. Auto-archive the staging PC unless the caller opted out.
            if not dry_run and not keep_staging and state.staging_channel is not None:
                self.archive_channel(state.staging_channel)

            return DocSyncResult(
                channel=target,
                preamble_ts=preamble_ts,
                thread_ts_by_slug=thread_ts_by_slug,
                thread_results=thread_results,
                deleted_slugs=[],
            )
        finally:
            self.channel = prev_channel

    # --- Doc-level pull ---
    def _resolve_user_name(self, user_id: str) -> str:
        """Look up ``user_id`` → username (Slack ``name`` field), cached per-client.

        Used to populate ``DocMessage.author`` on pull; the cache dedupes
        ``users.info`` calls when several messages share the same author.
        """
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]
        result = self._request("users.info", {"user": user_id}, method="GET")
        name = result["user"]["name"]
        self._user_name_cache[user_id] = name
        return name

    def _raw_to_doc_message(self, raw: dict) -> DocMessage:
        """Convert a raw Slack message dict → `DocMessage`.

        ``author=None`` for messages we authored (matched via ``bot_ids``);
        else the username resolved from ``users.info``.
        """
        user_id, bot_id = self.bot_ids
        ours = raw.get("user") == user_id or (bot_id is not None and raw.get("bot_id") == bot_id)
        content = raw.get("text", "")
        if ours:
            return DocMessage(content=content, author=None)
        return DocMessage(content=content, author=self._resolve_user_name(raw["user"]))

    def _pull_thread_docmessages(self, thread_ts: str) -> list[DocMessage]:
        """Fetch a thread's OP + replies from ``self.channel``, translate to DocMessages."""
        result = self._request("conversations.replies", {
            "channel": self.channel,
            "ts": thread_ts,
        }, method="GET")
        return [self._raw_to_doc_message(m) for m in result.get("messages", [])]

    def _pull_doc(
        self,
        channel: str,
        preamble_ts: str | None,
        thread_ts_by_slug: dict[str, str],
    ) -> Doc:
        """Fetch a doc's content from ``channel`` given the state pointers.

        Threads are returned in OP-ts numerical order (= channel post-order),
        which is stable regardless of how ``thread_ts_by_slug`` is iterated.
        """
        prev_channel = self.channel
        self.channel = channel
        try:
            preamble = None
            if preamble_ts is not None:
                msgs = self._pull_thread_docmessages(preamble_ts)
                if msgs:
                    # Preamble has no replies; the OP is the only message.
                    preamble = msgs[0].content

            sorted_slugs = sorted(thread_ts_by_slug, key=lambda s: float(thread_ts_by_slug[s]))
            threads = [
                DocThread(
                    slug=slug,
                    messages=self._pull_thread_docmessages(thread_ts_by_slug[slug]),
                )
                for slug in sorted_slugs
            ]
            return Doc(preamble=preamble, threads=threads)
        finally:
            self.channel = prev_channel

    def pull_doc_staging(self, state: SessionState) -> Doc:
        """Fetch the current state of the session's staging PC as a `Doc`.

        Uses the state's slug → ts pointers as the roots and ``conversations.replies``
        for each; foreign messages (colleague replies) come back with their
        ``author`` populated from ``users.info``.
        """
        if state.staging_channel is None:
            raise ValueError(
                "No staging channel — the session hasn't pushed a staging Doc yet."
            )
        return self._pull_doc(
            state.staging_channel,
            state.staging_preamble_ts,
            state.staging_threads,
        )

    def pull_doc_prod(
        self,
        state: SessionState,
        channel: str | None = None,
    ) -> Doc:
        """Fetch the current state of the doc on a real prod channel.

        ``channel`` overrides ``state.prod_channel`` if given; falls back to
        the pinned prod channel otherwise. Raises if neither is set.
        """
        target = channel if channel is not None else state.prod_channel
        if target is None:
            raise ValueError(
                "No prod channel — pass channel= or set state.prod_channel first."
            )
        return self._pull_doc(
            target,
            state.prod_preamble_ts.get(target),
            state.prod_threads.get(target, {}),
        )
