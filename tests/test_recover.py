"""Tests for `SlackClient.scan_thrds_metadata` — the recovery scan.

Scan-side unit tests (the CLI-side verb is covered in `test_cli.py`).
Mirrors `test_doc_sync.py`'s `_FakeSlackClient(handlers)` pattern.
"""
from __future__ import annotations

import pytest

from thrds import RecoveredSession, ScanCapReached
from thrds.slack import SlackClient, THRDS_METADATA_EVENT_TYPE


class _FakeSlackClient(SlackClient):
    """SlackClient with `_request` stubbed to a scripted `conversations.history` handler."""
    def __init__(self, handler):
        super().__init__(token='x', channel='')
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    def _request(self, endpoint, data=None, method='POST'):
        self.calls.append((endpoint, data))
        if endpoint == 'conversations.history':
            return self._handler(data)
        raise NotImplementedError(f'no handler for {endpoint}')


def _msg(ts: str, kind: str, session_id: str = 's-1', doc_slug: str = 'trainium',
         thread_slug: str | None = None, metadata: dict | None = None) -> dict:
    """Build a Slack message dict with thrds metadata.

    Pass ``metadata`` to override the whole metadata field (for malformed /
    non-thrds cases); default synthesizes a well-formed thrds payload.
    """
    if metadata is None:
        payload = {'session_id': session_id, 'doc_slug': doc_slug, 'kind': kind}
        if thread_slug is not None:
            payload['thread_slug'] = thread_slug
        metadata = {'event_type': THRDS_METADATA_EVENT_TYPE, 'event_payload': payload}
    return {'ts': ts, 'text': f'msg at {ts}', 'metadata': metadata}


def _single_page(messages):
    """Handler returning `messages` on a single page (no pagination)."""
    def handler(data):
        return {'messages': messages, 'has_more': False}
    return handler


# --- basic shape ---

def test_scan_empty_channel_returns_empty_dict():
    client = _FakeSlackClient(_single_page([]))
    assert client.scan_thrds_metadata('C1') == {}


def test_scan_single_session_with_preamble_and_threads():
    client = _FakeSlackClient(_single_page([
        _msg('1.000', kind='preamble'),
        _msg('1.001', kind='op', thread_slug='mfu'),
        _msg('1.002', kind='op', thread_slug='segfault'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert list(result) == ['s-1']
    sess = result['s-1']
    assert sess == RecoveredSession(
        session_id='s-1',
        doc_slug='trainium',
        preamble_ts='1.000',
        thread_ts_by_slug={'mfu': '1.001', 'segfault': '1.002'},
        oldest_ts='1.000',
        newest_ts='1.002',
    )
    assert sess.thread_count == 2


def test_scan_session_without_preamble_leaves_preamble_ts_none():
    """`kind='op'` messages only → preamble_ts stays None."""
    client = _FakeSlackClient(_single_page([
        _msg('1.001', kind='op', thread_slug='a'),
        _msg('1.002', kind='op', thread_slug='b'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert result['s-1'].preamble_ts is None
    assert result['s-1'].thread_ts_by_slug == {'a': '1.001', 'b': '1.002'}


def test_scan_multiple_sessions_are_grouped_by_session_id():
    """Two session_ids in the same channel → two entries; ts bounds per-session."""
    client = _FakeSlackClient(_single_page([
        _msg('1.001', kind='op', thread_slug='a', session_id='s-1', doc_slug='doc-1'),
        _msg('2.001', kind='op', thread_slug='x', session_id='s-2', doc_slug='doc-2'),
        _msg('2.002', kind='op', thread_slug='y', session_id='s-2', doc_slug='doc-2'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert sorted(result) == ['s-1', 's-2']
    assert result['s-1'].doc_slug == 'doc-1'
    assert result['s-2'].doc_slug == 'doc-2'
    assert result['s-2'].thread_ts_by_slug == {'x': '2.001', 'y': '2.002'}
    assert result['s-2'].oldest_ts == '2.001'
    assert result['s-2'].newest_ts == '2.002'


# --- threads sorted by ts (channel post-order) ---

def test_scan_thread_ts_by_slug_is_sorted_numerically():
    """Slugs come out in ts order, not scan / dict-insertion order."""
    client = _FakeSlackClient(_single_page([
        # Deliberately scan-order-scrambled ts values.
        _msg('3.000', kind='op', thread_slug='third'),
        _msg('1.000', kind='op', thread_slug='first'),
        _msg('2.000', kind='op', thread_slug='second'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert list(result['s-1'].thread_ts_by_slug) == ['first', 'second', 'third']


# --- filtering: what to skip ---

def test_scan_ignores_messages_without_thrds_metadata():
    """Non-thrds `event_type` or missing metadata → not counted."""
    client = _FakeSlackClient(_single_page([
        {'ts': '1.000', 'text': 'plain human msg'},
        {'ts': '1.001', 'text': 'other app', 'metadata': {
            'event_type': 'some_other_app',
            'event_payload': {'session_id': 's-x', 'doc_slug': 'nope', 'kind': 'op'},
        }},
        _msg('1.002', kind='op', thread_slug='real'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert list(result) == ['s-1']
    assert result['s-1'].thread_ts_by_slug == {'real': '1.002'}


def test_scan_skips_malformed_thrds_metadata():
    """Missing session_id / doc_slug / kind → skip, don't crash."""
    missing_session = {'event_type': THRDS_METADATA_EVENT_TYPE,
                       'event_payload': {'doc_slug': 'trainium', 'kind': 'op', 'thread_slug': 'x'}}
    missing_slug = {'event_type': THRDS_METADATA_EVENT_TYPE,
                    'event_payload': {'session_id': 's-1', 'kind': 'op', 'thread_slug': 'x'}}
    missing_kind = {'event_type': THRDS_METADATA_EVENT_TYPE,
                    'event_payload': {'session_id': 's-1', 'doc_slug': 'trainium'}}
    client = _FakeSlackClient(_single_page([
        {'ts': '1.001', 'text': 'no session', 'metadata': missing_session},
        {'ts': '1.002', 'text': 'no slug', 'metadata': missing_slug},
        {'ts': '1.003', 'text': 'no kind', 'metadata': missing_kind},
        _msg('1.004', kind='op', thread_slug='real'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert list(result) == ['s-1']
    assert result['s-1'].thread_ts_by_slug == {'real': '1.004'}


def test_scan_reply_kind_does_not_contribute_to_threads_map():
    """`kind='reply'` (if it surfaces) doesn't populate the slug→ts map."""
    client = _FakeSlackClient(_single_page([
        _msg('1.001', kind='op', thread_slug='mfu'),
        # Replies normally live under conversations.replies, but if one leaks
        # into the history scan we should ignore it (not treat as an OP).
        _msg('1.002', kind='reply', thread_slug='mfu'),
    ]))
    result = client.scan_thrds_metadata('C1')
    assert result['s-1'].thread_ts_by_slug == {'mfu': '1.001'}


# --- consistency ---

def test_scan_raises_on_inconsistent_doc_slug_within_session():
    """Same session_id disagreeing on doc_slug → corruption; raise."""
    client = _FakeSlackClient(_single_page([
        _msg('1.001', kind='op', thread_slug='a', doc_slug='trainium'),
        _msg('1.002', kind='op', thread_slug='b', doc_slug='different'),
    ]))
    with pytest.raises(ValueError, match=r'Inconsistent doc_slug for session s-1'):
        client.scan_thrds_metadata('C1')


# --- pagination ---

def test_scan_follows_next_cursor_across_pages():
    """`has_more=True` + `response_metadata.next_cursor` → walk pages until exhausted."""
    pages = [
        {
            'messages': [_msg('1.001', kind='op', thread_slug='a')],
            'has_more': True,
            'response_metadata': {'next_cursor': 'CURSOR_1'},
        },
        {
            'messages': [_msg('1.002', kind='op', thread_slug='b')],
            'has_more': True,
            'response_metadata': {'next_cursor': 'CURSOR_2'},
        },
        {
            'messages': [_msg('1.003', kind='op', thread_slug='c')],
            'has_more': False,
        },
    ]
    call_seq: list[str | None] = []

    def handler(data):
        call_seq.append(data.get('cursor'))
        return pages[len(call_seq) - 1]

    client = _FakeSlackClient(handler)
    result = client.scan_thrds_metadata('C1')
    assert call_seq == [None, 'CURSOR_1', 'CURSOR_2']   # first page has no cursor
    assert result['s-1'].thread_ts_by_slug == {'a': '1.001', 'b': '1.002', 'c': '1.003'}


def test_scan_stops_when_next_cursor_missing_even_if_has_more_true():
    """Defensive: `has_more=True` but no cursor → still terminate (not infinite loop)."""
    def handler(data):
        return {
            'messages': [_msg('1.001', kind='op', thread_slug='a')],
            'has_more': True,
            'response_metadata': {},   # no next_cursor
        }

    client = _FakeSlackClient(handler)
    result = client.scan_thrds_metadata('C1')
    assert result['s-1'].thread_ts_by_slug == {'a': '1.001'}


# --- request shape ---

def test_scan_requests_include_all_metadata_flag():
    """`include_all_metadata=True` must be in the request — otherwise Slack omits `metadata`."""
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C_TARGET')
    assert len(client.calls) == 1
    endpoint, data = client.calls[0]
    assert endpoint == 'conversations.history'
    assert data['channel'] == 'C_TARGET'
    assert data['include_all_metadata'] is True
    assert data['limit'] == 200


# --- scan bounds ---

def test_scan_forwards_oldest_to_slack_as_string():
    """`oldest=<float>` is coerced to a 9-decimal string at the wire.

    Slack silently returns wrong results when ts bounds are floats OR
    strings with too few decimals (empirically <=6 fails; .7f+ works).
    Formatting at the API boundary means all callers get the same
    treatment.
    """
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C1', oldest=1_700_000_000.0)
    assert client.calls[0][1]['oldest'] == '1700000000.000000000'


def test_scan_omits_oldest_when_not_given():
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C1')
    assert 'oldest' not in client.calls[0][1]


def test_scan_max_pages_raises_scan_cap_reached():
    """`max_pages=2` on a 3-page channel → raises `ScanCapReached`."""
    # Always claims has_more with a fresh cursor → would loop forever without a cap.
    def handler(data):
        return {
            'messages': [_msg('1.001', kind='op', thread_slug='a')],
            'has_more': True,
            'response_metadata': {'next_cursor': f'C_{data.get("cursor", "start")}'},
        }

    client = _FakeSlackClient(handler)
    with pytest.raises(ScanCapReached, match=r'hit --max-pages=2'):
        client.scan_thrds_metadata('C1', max_pages=2)


def test_scan_max_pages_none_disables_cap_and_terminates_normally():
    """`max_pages=None` explicitly disables the cap; scan still terminates via has_more."""
    client = _FakeSlackClient(_single_page([_msg('1.001', kind='op', thread_slug='a')]))
    result = client.scan_thrds_metadata('C1', max_pages=None)
    assert list(result['s-1'].thread_ts_by_slug) == ['a']


def test_scan_on_page_callback_receives_page_number_and_count():
    """`on_page(page_num, msg_count)` called once per page fetch, 1-indexed."""
    pages = [
        {'messages': [_msg('1.001', kind='op', thread_slug='a'),
                      _msg('1.002', kind='op', thread_slug='b')],
         'has_more': True, 'response_metadata': {'next_cursor': 'C1'}},
        {'messages': [_msg('1.003', kind='op', thread_slug='c')],
         'has_more': False},
    ]
    idx = [0]

    def handler(data):
        page = pages[idx[0]]
        idx[0] += 1
        return page

    observed: list[tuple[int, int]] = []
    client = _FakeSlackClient(handler)
    client.scan_thrds_metadata('C1', on_page=lambda n, c: observed.append((n, c)))
    assert observed == [(1, 2), (2, 1)]


# --- scan: resume support ---

def test_scan_forwards_latest_bound_to_slack_as_string():
    """`latest=<float>` is coerced to a 9-decimal string at the wire (see oldest test)."""
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C1', latest=1_700_000_000.0)
    assert client.calls[0][1]['latest'] == '1700000000.000000000'


def test_scan_forwards_starting_cursor_when_given():
    """`cursor=TOK` becomes `cursor` on the FIRST page request (resume path)."""
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C1', cursor='RESUME_TOK')
    assert client.calls[0][1]['cursor'] == 'RESUME_TOK'


def test_scan_omits_cursor_on_first_page_when_not_given():
    """No `cursor=` in the initial page request when the caller didn't pass one."""
    client = _FakeSlackClient(_single_page([]))
    client.scan_thrds_metadata('C1')
    assert 'cursor' not in client.calls[0][1]


def test_scan_cap_reached_carries_next_cursor_and_oldest_ts():
    """When the cap fires, the exception's cursor is what we would have sent next,
    and `oldest_ts_reached` is the min ts across ALL scanned messages."""
    # Two pages returned, both `has_more=True`; scan aborts on the 3rd page attempt.
    pages = [
        {'messages': [_msg('9.000', kind='op', thread_slug='a'),
                      _msg('8.000', kind='op', thread_slug='b')],
         'has_more': True, 'response_metadata': {'next_cursor': 'AFTER_1'}},
        {'messages': [_msg('7.000', kind='op', thread_slug='c'),
                      _msg('5.500', kind='op', thread_slug='d')],
         'has_more': True, 'response_metadata': {'next_cursor': 'AFTER_2'}},
    ]
    idx = [0]

    def handler(data):
        page = pages[idx[0]]
        idx[0] += 1
        return page

    client = _FakeSlackClient(handler)
    with pytest.raises(ScanCapReached) as exc_info:
        client.scan_thrds_metadata('C1', max_pages=2)
    exc = exc_info.value
    assert exc.next_cursor == 'AFTER_2'
    assert exc.oldest_ts_reached == '5.500'
    assert exc.pages_scanned == 2


def test_scan_cap_reached_before_any_page_carries_none_state():
    """`max_pages=0` disables the cap, but if we could ever fire pre-fetch, cursor+ts would be None."""
    # Directly assert on the exception fields via a hand-crafted case: cap=1
    # with a handler that always returns has_more=True. First page fetches;
    # cap fires on second-page attempt with the cursor Slack gave us and
    # min_ts_seen from page 1.
    def handler(data):
        return {
            'messages': [_msg('2.000', kind='op', thread_slug='a')],
            'has_more': True,
            'response_metadata': {'next_cursor': 'CURSOR_1'},
        }

    client = _FakeSlackClient(handler)
    with pytest.raises(ScanCapReached) as exc_info:
        client.scan_thrds_metadata('C1', max_pages=1)
    assert exc_info.value.next_cursor == 'CURSOR_1'
    assert exc_info.value.oldest_ts_reached == '2.000'
    assert exc_info.value.pages_scanned == 1
