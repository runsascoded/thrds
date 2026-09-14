"""Per-message sender (`+++ as <name>` / `op_sender`) through `slack push`/`promote`.

The doc syntax (`specs/doc-sender-syntax.md`) was live for `discord push` first;
these cover the Slack CLI push/promote paths reaching parity. The frontmatter's
sender profiles ride from `read_threads` into `sync_threads_staging` /
`promote_thread` (via `frontmatter_by_slug` / `frontmatter`), get resolved to
`Msg`, and reach `chat.postMessage` as `username` / `icon_url` / `icon_emoji` —
and survive the ref-placeholder / custom-emoji `DocMessage` rebuilds in between.

`core.sync`'s bot-token guard means per-sender needs an `xoxb-` token; a plain
push with no senders is unchanged (every post bare).
"""
from __future__ import annotations

from dataclasses import dataclass

from thrds import DocMessage, DocThread, SessionState, ThreadEntry, ThreadTarget
from thrds.doc import Frontmatter, SenderProfile
from thrds.slack import SlackClient


@dataclass
class Post:
    """One captured `chat.postMessage`: content + resolved sender fields."""
    text: str
    username: str | None
    icon_url: str | None
    icon_emoji: str | None


class FakeSlack(SlackClient):
    """`SlackClient` with `_request` scripted; records posts with sender fields."""

    def __init__(self, *, token: str = 'xoxb-test'):
        super().__init__(token=token, channel='C0S')
        self.posts: list[Post] = []
        self._recorded: list = []
        self._ts = iter(f'1.{i:06d}' for i in range(1, 100))

    def _request(self, endpoint, data=None, method='POST'):
        data = data or {}
        if endpoint == 'auth.test':
            return {'ok': True, 'user_id': 'U0ME', 'bot_id': 'B0ME'}
        if endpoint == 'conversations.replies':
            ts = data['ts']
            msgs = [{'ts': p_ts, 'text': p.text, 'user': 'U0ME'}
                    for p_ts, p in self._posted_by_thread(ts)]
            return {'ok': True, 'messages': msgs}
        if endpoint == 'chat.postMessage':
            ts = next(self._ts)
            self._recorded.append((ts, data.get('thread_ts'), Post(
                text=data['text'],
                username=data.get('username'),
                icon_url=data.get('icon_url'),
                icon_emoji=data.get('icon_emoji'),
            )))
            self.posts.append(self._recorded[-1][2])
            return {'ok': True, 'ts': ts}
        if endpoint in ('chat.update', 'chat.delete', 'conversations.archive'):
            return {'ok': True}
        if endpoint == 'chat.getPermalink':
            return {'ok': True, 'permalink': f"https://slack.example/{data['message_ts']}"}
        raise NotImplementedError(f"no handler for {endpoint}")

    def _posted_by_thread(self, thread_ts):
        return [(ts, p) for ts, tt, p in self._recorded
                if ts == thread_ts or tt == thread_ts]


def _client() -> FakeSlack:
    c = FakeSlack()
    c._bot_ids = ('U0ME', 'B0ME')
    return c


def _state(**kw) -> SessionState:
    base = dict(session_slug='s', staging_channel='C0S', threads={})
    base.update(kw)
    return SessionState.new(**base)


def _fm(**senders) -> Frontmatter:
    return Frontmatter(senders={
        name: SenderProfile(name=p[0], icon_url=p[1], icon_emoji=p[2])
        for name, p in senders.items()
    })


def test_staging_push_reply_as_custom_sender(tmp_path, monkeypatch):
    """A `+++ as alice` reply posts with alice's name + avatar; the OP stays bare."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    thread = DocThread(slug='a', messages=[
        DocMessage('OP body.'),
        DocMessage('Alice reply.', sender='alice'),
    ])
    fm = _fm(alice=('Alice', 'https://cdn/a.png', None))

    client.sync_threads_staging(
        [thread], _state(), pace=0.0, remote=None,
        frontmatter_by_slug={'a': fm},
    )

    assert client.posts == [
        Post('OP body.', None, None, None),
        Post('Alice reply.', 'Alice', 'https://cdn/a.png', None),
    ]


def test_staging_push_op_sender(tmp_path, monkeypatch):
    """`op_sender` posts the OP under that profile (Slack allows a custom OP sender)."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    thread = DocThread(slug='a', messages=[
        DocMessage('Digest OP.'),
        DocMessage('A reply.'),
    ])
    fm = _fm(digest=('GCS usage', None, ':bar_chart:'))
    fm.op_sender = 'digest'

    client.sync_threads_staging(
        [thread], _state(), pace=0.0, remote=None,
        frontmatter_by_slug={'a': fm},
    )

    assert client.posts == [
        Post('Digest OP.', 'GCS usage', None, ':bar_chart:'),
        Post('A reply.', None, None, None),
    ]


def test_staging_push_sender_survives_ref_substitution(tmp_path, monkeypatch):
    """A ref in the thread rebuilds every `DocMessage`; the reply's sender survives.

    `[link](#a)` makes `thread_has_refs` true, so `_prepare_doc_for_refs`
    substitutes and rebuilds the messages before the initial post. If that
    rebuild dropped `sender` (the bug), the reply would post bare.
    """
    monkeypatch.chdir(tmp_path)
    client = _client()
    threads = [
        DocThread(slug='a', messages=[DocMessage('Anchor OP.')]),
        DocThread(slug='b', messages=[
            DocMessage('See [the other](#a).'),
            DocMessage('Bob reply.', sender='bob'),
        ]),
    ]
    fm_b = _fm(bob=('Bob', 'https://cdn/b.png', None))

    client.sync_threads_staging(
        threads, _state(), pace=0.0, remote=None,
        frontmatter_by_slug={'b': fm_b},
    )

    bob = [p for p in client.posts if p.text == 'Bob reply.']
    assert bob == [Post('Bob reply.', 'Bob', 'https://cdn/b.png', None)]


def test_staging_push_no_senders_all_bare(tmp_path, monkeypatch):
    """Zero behavior change: a sender-free push posts every message bare."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    thread = DocThread(slug='a', messages=[
        DocMessage('OP.'),
        DocMessage('Reply.'),
    ])

    client.sync_threads_staging([thread], _state(), pace=0.0, remote=None)

    assert client.posts == [
        Post('OP.', None, None, None),
        Post('Reply.', None, None, None),
    ]


def test_promote_reply_as_custom_sender(tmp_path, monkeypatch):
    """`promote_thread` posts a `+++ as alice` reply under alice's identity."""
    monkeypatch.chdir(tmp_path)
    client = _client()
    thread = DocThread(slug='a', messages=[
        DocMessage('OP body.'),
        DocMessage('Alice reply.', sender='alice'),
    ])
    fm = _fm(alice=('Alice', 'https://cdn/a.png', None))
    state = _state(threads={'a': ThreadEntry()})
    target = ThreadTarget(channel='C0PROD')

    client.promote_thread('a', thread, target, state, pace=0.0, frontmatter=fm)

    assert client.posts == [
        Post('OP body.', None, None, None),
        Post('Alice reply.', 'Alice', 'https://cdn/a.png', None),
    ]


def test_resolve_custom_emoji_preserves_sender(tmp_path, monkeypatch):
    """The `_resolve_custom_emoji` `DocMessage` rebuild keeps `sender` set."""
    monkeypatch.chdir(tmp_path)
    from thrds.doc import Doc
    client = _client()
    doc = Doc(threads=[DocThread(slug='a', messages=[
        DocMessage('OP.'),
        DocMessage('Reply.', sender='alice'),
    ])])

    out = client._resolve_custom_emoji(doc, _state(), tmp_path, download=False)

    assert [(m.content, m.sender) for m in out.threads[0].messages] == [
        ('OP.', None),
        ('Reply.', 'alice'),
    ]
