"""Tests for `SlackClient.sync_doc_staging` (Phase B5).

Uses `_FakeSlackClient` (mirrors `tests/test_transport.py`) to script the
Slack API responses; asserts on posted content, channel routing, state
mutation, and terraform semantics.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from thrds import Doc, DocMessage, DocThread, SessionState
from thrds.slack import SlackClient


class _FakeSlackClient(SlackClient):
    """SlackClient with `_request` stubbed to a scripted dispatcher."""
    def __init__(self, handlers):
        super().__init__(token="x", channel="C_INIT")
        self._handlers = handlers
        self.calls: list[tuple[str, dict, str]] = []

    def _request(self, endpoint, data=None, method="POST"):
        self.calls.append((endpoint, data, method))
        handler = self._handlers.get(endpoint)
        if handler is None:
            raise NotImplementedError(f"no handler for {endpoint}")
        return handler(data, method)


@dataclass
class Posted:
    ts: str
    channel: str
    text: str
    thread_ts: str | None
    metadata: dict | None


def _make_client(*, existing_replies: dict[str, list[dict]] | None = None):
    """Build a fake client with the default handler set for a staging sync.

    ``existing_replies`` maps thread_ts → list of message dicts that
    ``conversations.replies`` should return for that thread. Absent thread_ts
    → empty. Every call is recorded; posts/edits/deletes accumulate in the
    returned tracker.

    A `chat.getPermalink` handler is included by default (returns a
    predictable `https://slack.example/<ts>` for any ts) so ref-resolution
    tests don't have to script it separately.
    """
    existing = existing_replies or {}
    ts_counter = iter(f"1.{i:06d}" for i in range(100))
    posted: list[Posted] = []
    edited: list[dict] = []
    deleted: list[str] = []
    created_channels: list[dict] = []
    archived_channels: list[str] = []

    def on_auth(_data, _method):
        return {"ok": True, "user_id": "U_ME", "bot_id": None}

    def on_create(data, _method):
        created_channels.append(data)
        return {"ok": True, "channel": {"id": "C_STAGING", "name": data["name"]}}

    def on_archive(data, _method):
        archived_channels.append(data["channel"])
        return {"ok": True}

    def on_replies(data, _method):
        """Seeded fixture wins; else reconstruct thread from `posted` + `edited`.

        Phase 3 needs to see phase-2 posts as "existing" so its edit-in-place
        diff works; a static empty return would make sync() re-post instead
        of edit. Edits mutate the recorded text so subsequent list_messages
        (rare) see the current state.
        """
        ts = data["ts"]
        if ts in existing:
            return {"ok": True, "messages": existing[ts]}
        op = next((p for p in posted if p.ts == ts), None)
        if op is None:
            return {"ok": True, "messages": []}
        latest_text = {e["ts"]: e["text"] for e in edited}
        msgs = [{"ts": op.ts, "text": latest_text.get(op.ts, op.text), "user": "U_ME"}]
        for r in posted:
            if r.thread_ts == ts and r.ts != ts:
                msgs.append({"ts": r.ts, "text": latest_text.get(r.ts, r.text), "user": "U_ME"})
        return {"ok": True, "messages": msgs}

    def on_post(data, _method):
        ts = next(ts_counter)
        posted.append(Posted(
            ts=ts,
            channel=data["channel"],
            text=data["text"],
            thread_ts=data.get("thread_ts"),
            metadata=data.get("metadata"),
        ))
        return {"ok": True, "ts": ts}

    def on_edit(data, _method):
        edited.append({
            "channel": data["channel"],
            "ts": data["ts"],
            "text": data["text"],
            "metadata": data.get("metadata"),
        })
        return {"ok": True}

    def on_delete(data, _method):
        deleted.append(data["ts"])
        return {"ok": True}

    def on_permalink(data, _method):
        return {"ok": True, "permalink": f"https://slack.example/{data['message_ts']}"}

    client = _FakeSlackClient({
        "auth.test": on_auth,
        "conversations.create": on_create,
        "conversations.archive": on_archive,
        "conversations.replies": on_replies,
        "chat.postMessage": on_post,
        "chat.update": on_edit,
        "chat.delete": on_delete,
        "chat.getPermalink": on_permalink,
    })
    return client, posted, edited, deleted, created_channels, archived_channels


def _basic_doc() -> Doc:
    return Doc(
        preamble="Sorry for the delay, updates below:",
        threads=[
            DocThread(slug="mfu", messages=[
                DocMessage("1. Latest MFU."),
                DocMessage("Reply about the chip-wide measurement."),
            ]),
            DocThread(slug="prof", messages=[
                DocMessage("2. Profiling / improving MFU"),
            ]),
        ],
    )


def _new_state(tmp_path, monkeypatch, **overrides) -> SessionState:
    """Fresh session state whose save()s land in tmp_path/thrds.json."""
    monkeypatch.chdir(tmp_path)
    return SessionState.new(doc_path='trainium-update.md', **overrides)


# --- fresh-session sync ---

def test_fresh_sync_creates_pc_and_posts_preamble_and_threads(tmp_path, monkeypatch):
    """Empty state: creates PC, posts preamble + each thread, records everything."""
    monkeypatch.setenv('THRDS_CHANNEL_PREFIX', 'rw-')
    state = _new_state(tmp_path, monkeypatch)
    client, posted, edited, deleted, created, archived = _make_client()

    result = client.sync_doc_staging(_basic_doc(), state, pace=0.0)

    # PC creation happened with prefix + doc slug.
    assert created == [{"name": "rw-trainium-update", "is_private": True}]
    assert state.staging_channel == "C_STAGING"
    assert archived == []
    assert edited == []
    assert deleted == []

    # Posts, in order: preamble, mfu OP, mfu reply, prof OP.
    assert [(p.text, p.thread_ts) for p in posted] == [
        ("Sorry for the delay, updates below:", None),
        ("1. Latest MFU.", None),
        ("Reply about the chip-wide measurement.", posted[1].ts),
        ("2. Profiling / improving MFU", None),
    ]
    assert {p.channel for p in posted} == {"C_STAGING"}

    # State captured all owned ts's.
    assert state.staging_preamble_ts == posted[0].ts
    assert state.staging_threads == {"mfu": posted[1].ts, "prof": posted[3].ts}
    assert result.channel == "C_STAGING"
    assert result.preamble_ts == posted[0].ts
    assert result.thread_ts_by_slug == {"mfu": posted[1].ts, "prof": posted[3].ts}
    assert result.deleted_slugs == []

    # Init-channel is restored after the sync (self.channel swap is scoped).
    assert client.channel == "C_INIT"


def test_fresh_sync_stamps_thrds_metadata_on_every_post(tmp_path, monkeypatch):
    """Every posted message carries `event_type='thrds'` + full payload."""
    state = _new_state(tmp_path, monkeypatch)
    client, posted, *_ = _make_client()

    client.sync_doc_staging(_basic_doc(), state, pace=0.0)

    kinds = [(p.metadata["event_type"], p.metadata["event_payload"]) for p in posted]
    assert kinds == [
        ("thrds", {
            "session_id": state.session_id,
            "doc_slug": "trainium-update",
            "kind": "preamble",
        }),
        ("thrds", {
            "session_id": state.session_id,
            "doc_slug": "trainium-update",
            "kind": "op",
            "thread_slug": "mfu",
        }),
        ("thrds", {
            "session_id": state.session_id,
            "doc_slug": "trainium-update",
            "kind": "reply",
            "thread_slug": "mfu",
        }),
        ("thrds", {
            "session_id": state.session_id,
            "doc_slug": "trainium-update",
            "kind": "op",
            "thread_slug": "prof",
        }),
    ]


def test_fresh_sync_persists_state_after_pc_create_and_after_full_sync(tmp_path, monkeypatch):
    """state.json is written at least twice: once after PC create, once at end."""
    state = _new_state(tmp_path, monkeypatch)
    client, *_ = _make_client()

    client.sync_doc_staging(_basic_doc(), state, pace=0.0)

    # Reload from disk — the on-disk state matches in-memory (final save).
    reloaded = SessionState.load(tmp_path)
    assert reloaded == state


# --- edit-in-place ---

def test_sync_edits_changed_op_in_place_when_slug_already_tracked(tmp_path, monkeypatch):
    """Slug in state → sync reuses OP ts, edits changed content."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_preamble_ts = "1.111000"
    state.staging_threads = {"mfu": "1.111001"}

    # conversations.replies for the mfu thread: existing OP + one reply, all ours.
    existing_replies = {
        "1.111001": [
            {"ts": "1.111001", "text": "OLD OP.", "user": "U_ME"},
            {"ts": "1.111002", "text": "OLD reply.", "user": "U_ME"},
        ],
        "1.111000": [
            {"ts": "1.111000", "text": "OLD preamble.", "user": "U_ME"},
        ],
    }
    client, posted, edited, deleted, created, archived = _make_client(existing_replies=existing_replies)

    doc = Doc(
        preamble="NEW preamble.",
        threads=[
            DocThread(slug="mfu", messages=[
                DocMessage("NEW OP."),
                DocMessage("NEW reply."),
            ]),
        ],
    )
    result = client.sync_doc_staging(doc, state, pace=0.0)

    assert created == []  # PC already exists
    assert deleted == []  # nothing terraform-deleted
    assert [p.text for p in posted] == []  # nothing new to post
    # Preamble edited + both mfu messages edited.
    assert [(e["ts"], e["text"]) for e in edited] == [
        ("1.111000", "NEW preamble."),
        ("1.111001", "NEW OP."),
        ("1.111002", "NEW reply."),
    ]
    assert state.staging_preamble_ts == "1.111000"
    assert state.staging_threads == {"mfu": "1.111001"}
    assert result.deleted_slugs == []


# --- terraform delete ---

def test_stale_slug_thread_is_deleted_from_state_and_slack(tmp_path, monkeypatch):
    """Slug tracked in state but absent from doc → thread deleted (staging = terraform)."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_threads = {"stale": "1.222001", "keep": "1.222010"}

    existing_replies = {
        "1.222001": [
            {"ts": "1.222001", "text": "Stale OP.", "user": "U_ME"},
            {"ts": "1.222002", "text": "Stale reply.", "user": "U_ME"},
        ],
        "1.222010": [
            {"ts": "1.222010", "text": "Keep OP.", "user": "U_ME"},
        ],
    }
    client, posted, edited, deleted, *_ = _make_client(existing_replies=existing_replies)

    doc = Doc(threads=[
        DocThread(slug="keep", messages=[DocMessage("Keep OP.")]),
    ])
    result = client.sync_doc_staging(doc, state, pace=0.0)

    # Both stale messages deleted, reply-then-OP order.
    assert deleted == ["1.222002", "1.222001"]
    # 'keep' unchanged (content matches → no edit needed).
    assert edited == []
    assert posted == []
    assert state.staging_threads == {"keep": "1.222010"}
    assert result.deleted_slugs == ["stale"]


def test_preamble_removed_from_doc_deletes_the_preamble_message(tmp_path, monkeypatch):
    """Doc no longer has a preamble → the tracked preamble ts is deleted."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_preamble_ts = "1.333000"
    state.staging_threads = {"a": "1.333001"}

    existing_replies = {"1.333001": [{"ts": "1.333001", "text": "OP.", "user": "U_ME"}]}
    client, posted, edited, deleted, *_ = _make_client(existing_replies=existing_replies)

    doc = Doc(preamble=None, threads=[DocThread(slug="a", messages=[DocMessage("OP.")])])
    client.sync_doc_staging(doc, state, pace=0.0)

    assert "1.333000" in deleted
    assert state.staging_preamble_ts is None


# --- foreign-msg preservation ---

def test_foreign_reply_on_slack_is_preserved_across_sync(tmp_path, monkeypatch):
    """Non-editable msgs (foreign author) stay in place — sync doesn't touch them."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_threads = {"mfu": "1.444001"}

    # Existing thread has our OP + our reply + a foreign reply (different user).
    existing_replies = {
        "1.444001": [
            {"ts": "1.444001", "text": "OP.", "user": "U_ME"},
            {"ts": "1.444002", "text": "Our reply.", "user": "U_ME"},
            {"ts": "1.444003", "text": "Grayjh's take.", "user": "U_GRAYJH"},
        ],
    }
    client, posted, edited, deleted, *_ = _make_client(existing_replies=existing_replies)

    # Doc carries the foreign reply for round-trip fidelity, but sync should
    # skip it (already on Slack, non-editable).
    doc = Doc(threads=[
        DocThread(slug="mfu", messages=[
            DocMessage("OP.", author=None),
            DocMessage("Our reply.", author=None),
            DocMessage("Grayjh's take.", author="grayjh"),
        ]),
    ])
    client.sync_doc_staging(doc, state, pace=0.0)

    assert deleted == []  # foreign never deleted
    assert edited == []   # ours match existing content
    assert posted == []


# --- dry_run ---

def test_dry_run_makes_no_api_calls_other_than_reads(tmp_path, monkeypatch):
    """dry_run: no PC create, no post/edit/delete; state unchanged."""
    state = _new_state(tmp_path, monkeypatch)
    doc = _basic_doc()
    client, posted, edited, deleted, created, archived = _make_client()

    result = client.sync_doc_staging(doc, state, dry_run=True, pace=0.0)

    assert created == []
    assert posted == []
    assert edited == []
    assert deleted == []
    assert archived == []
    # State didn't get a real PC.
    assert state.staging_channel is None
    assert state.staging_preamble_ts is None
    assert state.staging_threads == {}
    # Result uses the placeholder channel.
    assert result.channel == "<new-pc>"


# --- validation ---

def test_slugless_thread_raises(tmp_path, monkeypatch):
    """Every thread must have a slug — state is slug-keyed."""
    state = _new_state(tmp_path, monkeypatch)
    doc = Doc(threads=[DocThread(slug=None, messages=[DocMessage("OP.")])])
    client, *_ = _make_client()
    with pytest.raises(ValueError, match="requires every thread to have a slug"):
        client.sync_doc_staging(doc, state)


# --- archive helper ---

def test_archive_channel_calls_conversations_archive():
    """archive_channel wraps conversations.archive with the channel_id."""
    client, *_, archived = _make_client()
    client.archive_channel("C_STAGING")
    assert archived == ["C_STAGING"]


# --- sync_doc_prod ---

def test_prod_sync_fresh_posts_to_target_channel(tmp_path, monkeypatch):
    """First prod push: nothing in state.prod_threads yet → posts everything to target."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_threads = {"mfu": "1.999001", "prof": "1.999002"}

    client, posted, edited, deleted, created, archived = _make_client()

    result = client.sync_doc_prod(_basic_doc(), state, channel="C_PROD", pace=0.0)

    # No PC creation (we're not staging).
    assert created == []
    # Posted preamble + 2 threads (with 1 reply on mfu) → 4 posts.
    assert [(p.text, p.channel, p.thread_ts) for p in posted] == [
        ("Sorry for the delay, updates below:", "C_PROD", None),
        ("1. Latest MFU.", "C_PROD", None),
        ("Reply about the chip-wide measurement.", "C_PROD", posted[1].ts),
        ("2. Profiling / improving MFU", "C_PROD", None),
    ]
    # State pins the resolved prod channel + records per-channel prod threads.
    assert state.prod_channel == "C_PROD"
    assert state.prod_threads == {"C_PROD": {"mfu": posted[1].ts, "prof": posted[3].ts}}
    assert state.prod_preamble_ts == {"C_PROD": posted[0].ts}
    # Staging state (channel + threads) preserved as history.
    assert state.staging_channel == "C_STAGING"
    assert state.staging_threads == {"mfu": "1.999001", "prof": "1.999002"}
    # Auto-archive fired on prod push (default keep_staging=False).
    assert archived == ["C_STAGING"]
    assert result.channel == "C_PROD"
    assert result.deleted_slugs == []


def test_prod_sync_keep_staging_skips_archive(tmp_path, monkeypatch):
    """keep_staging=True: no archive call, PC left as-is."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"

    client, posted, edited, deleted, created, archived = _make_client()

    client.sync_doc_prod(_basic_doc(), state, channel="C_PROD", keep_staging=True, pace=0.0)

    assert archived == []
    # Staging channel still tracked; caller can archive it later.
    assert state.staging_channel == "C_STAGING"


def test_prod_sync_is_additive_stale_slug_in_state_is_not_deleted(tmp_path, monkeypatch):
    """Slug in state.prod_threads but absent from doc: LEFT in place (additive)."""
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_PROD"
    state.prod_threads = {"C_PROD": {"gone": "1.777001", "mfu": "1.777002", "prof": "1.777003"}}

    existing_replies = {
        "1.777002": [{"ts": "1.777002", "text": "1. Latest MFU.", "user": "U_ME"}, {"ts": "1.777002r", "text": "Reply about the chip-wide measurement.", "user": "U_ME"}],
        "1.777003": [{"ts": "1.777003", "text": "2. Profiling / improving MFU", "user": "U_ME"}],
    }
    client, posted, edited, deleted, created, archived = _make_client(existing_replies=existing_replies)

    result = client.sync_doc_prod(_basic_doc(), state, pace=0.0)

    assert deleted == []  # 'gone' NOT deleted
    assert result.deleted_slugs == []
    # 'gone' still in state after the push.
    assert "gone" in state.prod_threads["C_PROD"]


def test_prod_sync_dry_run_makes_no_writes(tmp_path, monkeypatch):
    """dry_run=True: nothing posted, edited, deleted, or archived; state unchanged."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"

    client, posted, edited, deleted, created, archived = _make_client()

    client.sync_doc_prod(_basic_doc(), state, channel="C_PROD", dry_run=True, pace=0.0)

    assert posted == []
    assert edited == []
    assert deleted == []
    assert archived == []
    assert state.prod_channel is None
    assert state.prod_threads == {}


def test_prod_sync_raises_when_no_channel_and_state_prod_channel_unset(tmp_path, monkeypatch):
    """Neither ``channel=`` nor ``state.prod_channel`` set → hard error."""
    state = _new_state(tmp_path, monkeypatch)
    client, *_ = _make_client()
    with pytest.raises(ValueError, match="No prod channel"):
        client.sync_doc_prod(_basic_doc(), state)


def test_prod_sync_preserves_existing_preamble_when_doc_dropped_it(tmp_path, monkeypatch):
    """Doc.preamble=None + existing prod preamble → preamble NOT deleted (additive)."""
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_PROD"
    state.prod_preamble_ts = {"C_PROD": "1.666000"}
    state.prod_threads = {"C_PROD": {"mfu": "1.666001"}}

    existing_replies = {
        "1.666001": [{"ts": "1.666001", "text": "OP.", "user": "U_ME"}],
    }
    client, posted, edited, deleted, *_ = _make_client(existing_replies=existing_replies)

    doc = Doc(preamble=None, threads=[DocThread(slug="mfu", messages=[DocMessage("OP.")])])
    client.sync_doc_prod(doc, state, pace=0.0)

    assert deleted == []  # existing preamble NOT deleted (additive)
    assert state.prod_preamble_ts == {"C_PROD": "1.666000"}  # preserved


def test_prod_sync_reuses_state_prod_channel_when_channel_arg_omitted(tmp_path, monkeypatch):
    """No channel= arg → uses state.prod_channel."""
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_PINNED"

    client, posted, *_ = _make_client()
    client.sync_doc_prod(_basic_doc(), state, pace=0.0)

    assert {p.channel for p in posted} == {"C_PINNED"}
    assert state.prod_channel == "C_PINNED"


# --- pull ---

def _make_pull_client(*, thread_replies: dict[str, list[dict]], users: dict[str, str] | None = None):
    """Fake client for pull tests.

    ``thread_replies``: thread_ts → list of raw Slack msg dicts to return
    from conversations.replies.

    ``users``: user_id → username (for users.info lookups). Missing IDs raise.
    """
    users = users or {}
    users_info_calls: list[str] = []

    def on_auth(_data, _method):
        return {"ok": True, "user_id": "U_ME", "bot_id": None}

    def on_replies(data, _method):
        return {"ok": True, "messages": thread_replies.get(data["ts"], [])}

    def on_users_info(data, _method):
        uid = data["user"]
        users_info_calls.append(uid)
        if uid not in users:
            raise NotImplementedError(f"no user data scripted for {uid}")
        return {"ok": True, "user": {"name": users[uid]}}

    client = _FakeSlackClient({
        "auth.test": on_auth,
        "conversations.replies": on_replies,
        "users.info": on_users_info,
    })
    return client, users_info_calls


def test_pull_staging_reconstructs_preamble_and_threads(tmp_path, monkeypatch):
    """Basic pull: preamble + 2 threads (all ours) → Doc with correct shape."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_preamble_ts = "1.000000"
    state.staging_threads = {"mfu": "1.000001", "prof": "1.000003"}

    thread_replies = {
        "1.000000": [{"ts": "1.000000", "text": "Preamble text.", "user": "U_ME"}],
        "1.000001": [
            {"ts": "1.000001", "text": "OP mfu.", "user": "U_ME"},
            {"ts": "1.000002", "text": "Reply mfu.", "user": "U_ME"},
        ],
        "1.000003": [{"ts": "1.000003", "text": "OP prof.", "user": "U_ME"}],
    }
    client, _ = _make_pull_client(thread_replies=thread_replies)

    doc = client.pull_doc_staging(state)

    assert doc == Doc(
        preamble="Preamble text.",
        threads=[
            DocThread(slug="mfu", messages=[
                DocMessage(content="OP mfu.", author=None),
                DocMessage(content="Reply mfu.", author=None),
            ]),
            DocThread(slug="prof", messages=[DocMessage(content="OP prof.", author=None)]),
        ],
    )
    # Client's channel restored after the pull.
    assert client.channel == "C_INIT"


def test_pull_populates_author_on_foreign_reply(tmp_path, monkeypatch):
    """Foreign reply (user != our bot_ids) → DocMessage.author = looked-up username."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_threads = {"mfu": "1.000001"}

    thread_replies = {
        "1.000001": [
            {"ts": "1.000001", "text": "OP.", "user": "U_ME"},
            {"ts": "1.000002", "text": "Interesting take.", "user": "U_GRAYJH"},
            {"ts": "1.000003", "text": "Follow-up.", "user": "U_ME"},
        ],
    }
    client, users_info_calls = _make_pull_client(
        thread_replies=thread_replies,
        users={"U_GRAYJH": "grayjh"},
    )

    doc = client.pull_doc_staging(state)

    assert doc.threads[0].messages == [
        DocMessage(content="OP.", author=None),
        DocMessage(content="Interesting take.", author="grayjh"),
        DocMessage(content="Follow-up.", author=None),
    ]
    assert users_info_calls == ["U_GRAYJH"]


def test_pull_caches_user_lookups_across_messages(tmp_path, monkeypatch):
    """The same foreign user appearing twice → one users.info call, not two."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    state.staging_threads = {"a": "1.000001", "b": "1.000010"}

    thread_replies = {
        "1.000001": [
            {"ts": "1.000001", "text": "OP a.", "user": "U_ME"},
            {"ts": "1.000002", "text": "grayjh weighs in.", "user": "U_GRAYJH"},
        ],
        "1.000010": [
            {"ts": "1.000010", "text": "OP b.", "user": "U_ME"},
            {"ts": "1.000011", "text": "grayjh again.", "user": "U_GRAYJH"},
        ],
    }
    client, users_info_calls = _make_pull_client(
        thread_replies=thread_replies,
        users={"U_GRAYJH": "grayjh"},
    )

    client.pull_doc_staging(state)
    assert users_info_calls == ["U_GRAYJH"]  # one call, cached across threads


def test_pull_orders_threads_by_ts_regardless_of_state_dict_order(tmp_path, monkeypatch):
    """Threads returned in ts (post-order), not the state dict's insertion order."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"
    # Insert in reverse-ts order to prove the sort matters.
    state.staging_threads = {"third": "1.000030", "first": "1.000010", "second": "1.000020"}

    thread_replies = {
        "1.000010": [{"ts": "1.000010", "text": "OP first.", "user": "U_ME"}],
        "1.000020": [{"ts": "1.000020", "text": "OP second.", "user": "U_ME"}],
        "1.000030": [{"ts": "1.000030", "text": "OP third.", "user": "U_ME"}],
    }
    client, _ = _make_pull_client(thread_replies=thread_replies)

    doc = client.pull_doc_staging(state)
    assert [t.slug for t in doc.threads] == ["first", "second", "third"]


def test_pull_empty_state_returns_empty_doc(tmp_path, monkeypatch):
    """staging_channel set but no threads / no preamble → Doc with no threads and preamble=None."""
    state = _new_state(tmp_path, monkeypatch)
    state.staging_channel = "C_STAGING"

    client, _ = _make_pull_client(thread_replies={})
    doc = client.pull_doc_staging(state)
    assert doc == Doc(preamble=None, threads=[])


def test_pull_staging_raises_when_no_staging_channel(tmp_path, monkeypatch):
    """staging_channel is None → clear error, no API call."""
    state = _new_state(tmp_path, monkeypatch)
    client, _ = _make_pull_client(thread_replies={})
    with pytest.raises(ValueError, match="No staging channel"):
        client.pull_doc_staging(state)


def test_pull_prod_uses_state_prod_channel_when_channel_arg_omitted(tmp_path, monkeypatch):
    """pull_doc_prod(state) with no explicit channel → uses state.prod_channel."""
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_PROD"
    state.prod_preamble_ts = {"C_PROD": "1.000000"}
    state.prod_threads = {"C_PROD": {"mfu": "1.000001"}}

    thread_replies = {
        "1.000000": [{"ts": "1.000000", "text": "Prod preamble.", "user": "U_ME"}],
        "1.000001": [{"ts": "1.000001", "text": "Prod OP.", "user": "U_ME"}],
    }
    client, _ = _make_pull_client(thread_replies=thread_replies)

    doc = client.pull_doc_prod(state)
    assert doc == Doc(
        preamble="Prod preamble.",
        threads=[DocThread(slug="mfu", messages=[DocMessage(content="Prod OP.", author=None)])],
    )


def test_pull_prod_channel_arg_overrides_state(tmp_path, monkeypatch):
    """pull_doc_prod(state, channel=X) fetches from X even if state.prod_channel differs."""
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_DEFAULT"
    state.prod_threads = {
        "C_DEFAULT": {"a": "1.000001"},
        "C_OTHER": {"b": "1.000002"},
    }

    thread_replies = {
        "1.000002": [{"ts": "1.000002", "text": "OP b.", "user": "U_ME"}],
    }
    client, _ = _make_pull_client(thread_replies=thread_replies)

    doc = client.pull_doc_prod(state, channel="C_OTHER")
    assert [t.slug for t in doc.threads] == ["b"]


def test_pull_prod_raises_without_channel(tmp_path, monkeypatch):
    """No channel= and no state.prod_channel → hard error."""
    state = _new_state(tmp_path, monkeypatch)
    client, _ = _make_pull_client(thread_replies={})
    with pytest.raises(ValueError, match="No prod channel"):
        client.pull_doc_prod(state)


# --- cross-thread refs (phase-2 placeholder / phase-3 real URL) ---

def test_cross_ref_phase2_posts_placeholder_phase3_edits_real_url(tmp_path, monkeypatch):
    """Ref-containing msg: phase-2 posts with placeholder; phase-3 edits to real URL."""
    from thrds.refs import PLACEHOLDER_URL
    state = _new_state(tmp_path, monkeypatch)
    doc = Doc(threads=[
        DocThread(slug="mfu", messages=[
            DocMessage("OP mfu."),
            DocMessage("See the [profiling thread](#prof) for methodology."),
        ]),
        DocThread(slug="prof", messages=[DocMessage("OP prof.")]),
    ])
    client, posted, edited, deleted, *_ = _make_client()

    client.sync_doc_staging(doc, state, pace=0.0)

    # Phase 2: 3 posts (no preamble in this doc):
    #   posted[0] = mfu OP,  posted[1] = mfu reply w/ placeholder,  posted[2] = prof OP.
    # Post texts are in Slack mrkdwn (`<url|text>`) — the client converts on the wire.
    assert posted[0].text == "OP mfu."
    assert posted[1].text == f"See the <{PLACEHOLDER_URL}|profiling thread> for methodology."
    assert posted[2].text == "OP prof."

    # Phase 3: only the ref-containing message is edited (real URL, also converted).
    ref_msg_ts = posted[1].ts
    prof_op_ts = posted[2].ts
    real_url = f"https://slack.example/{prof_op_ts}"
    assert [(e["ts"], e["text"]) for e in edited] == [
        (ref_msg_ts, f"See the <{real_url}|profiling thread> for methodology."),
    ]


def test_no_refs_skips_phase3_entirely(tmp_path, monkeypatch):
    """Doc without refs: no chat.getPermalink calls, no re-sync passes."""
    state = _new_state(tmp_path, monkeypatch)
    doc = _basic_doc()  # no refs
    client, posted, edited, *_ = _make_client()

    client.sync_doc_staging(doc, state, pace=0.0)

    # Zero edits in phase 3 — refs never introduced any.
    permalink_calls = [c for c in client.calls if c[0] == "chat.getPermalink"]
    assert permalink_calls == []
    assert edited == []


def test_dry_run_with_refs_makes_no_api_calls(tmp_path, monkeypatch):
    """Dry-run with refs still validates but never substitutes/posts/edits."""
    state = _new_state(tmp_path, monkeypatch)
    doc = Doc(threads=[
        DocThread(slug="mfu", messages=[DocMessage("See [prof](#prof).")]),
        DocThread(slug="prof", messages=[DocMessage("OP.")]),
    ])
    client, posted, edited, deleted, created, archived = _make_client()

    client.sync_doc_staging(doc, state, dry_run=True, pace=0.0)

    assert created == []
    assert posted == []
    assert edited == []
    permalink_calls = [c for c in client.calls if c[0] == "chat.getPermalink"]
    assert permalink_calls == []


def test_dangling_cross_ref_raises_before_any_api_call(tmp_path, monkeypatch):
    """A ref to a non-existent slug fails validation, no PC created, no posts."""
    state = _new_state(tmp_path, monkeypatch)
    doc = Doc(threads=[
        DocThread(slug="mfu", messages=[DocMessage("See [gone](#nope).")]),
    ])
    client, posted, edited, deleted, created, archived = _make_client()

    with pytest.raises(ValueError, match=r"#nope"):
        client.sync_doc_staging(doc, state)

    assert created == []
    assert posted == []


def test_preamble_with_cross_ref_resolves(tmp_path, monkeypatch):
    """A ref in the preamble is substituted phase-2 and edited phase-3 too."""
    from thrds.refs import PLACEHOLDER_URL
    state = _new_state(tmp_path, monkeypatch)
    doc = Doc(
        preamble="Read the [MFU thread](#mfu) first.",
        threads=[DocThread(slug="mfu", messages=[DocMessage("OP.")])],
    )
    client, posted, edited, *_ = _make_client()

    client.sync_doc_staging(doc, state, pace=0.0)

    # Phase 2: preamble posted with placeholder (in Slack mrkdwn on the wire).
    assert posted[0].text == f"Read the <{PLACEHOLDER_URL}|MFU thread> first."
    # Phase 3: preamble edited with real URL.
    mfu_op_ts = posted[1].ts
    preamble_ts = posted[0].ts
    real_url = f"https://slack.example/{mfu_op_ts}"
    edit_preamble = next(e for e in edited if e["ts"] == preamble_ts)
    assert edit_preamble["text"] == f"Read the <{real_url}|MFU thread> first."


def test_message_too_long_after_placeholder_substitution_raises(tmp_path, monkeypatch):
    """A message that fits raw but overflows after placeholder substitution → clear error."""
    from thrds.slack import SLACK_MESSAGE_LIMIT
    state = _new_state(tmp_path, monkeypatch)
    # 3900-char body + a single ref (~180-char placeholder inflation).
    filler = "a" * 3900
    doc = Doc(threads=[
        DocThread(slug="mfu", messages=[DocMessage(f"{filler} [prof](#prof)")]),
        DocThread(slug="prof", messages=[DocMessage("OP.")]),
    ])
    client, posted, *_ = _make_client()

    with pytest.raises(ValueError, match=r"ref-placeholder substitution"):
        client.sync_doc_staging(doc, state)

    # Nothing posted — the length check runs before phase 1.
    assert posted == []


def test_prod_cross_ref_resolves_via_permalink(tmp_path, monkeypatch):
    """sync_doc_prod also runs phase 3: ref-msg posted w/ placeholder, edited w/ real URL."""
    from thrds.refs import PLACEHOLDER_URL
    state = _new_state(tmp_path, monkeypatch)
    state.prod_channel = "C_PROD"
    doc = Doc(threads=[
        DocThread(slug="a", messages=[DocMessage("See [b](#b).")]),
        DocThread(slug="b", messages=[DocMessage("OP b.")]),
    ])
    client, posted, edited, *_ = _make_client()

    client.sync_doc_prod(doc, state, pace=0.0)

    # Slack mrkdwn on the wire (`<url|text>`).
    assert posted[0].text == f"See <{PLACEHOLDER_URL}|b>."
    b_op_ts = posted[1].ts
    real_url = f"https://slack.example/{b_op_ts}"
    # Phase 3 edits the ref-containing msg with the real URL.
    assert [(e["ts"], e["text"]) for e in edited] == [
        (posted[0].ts, f"See <{real_url}|b>."),
    ]
