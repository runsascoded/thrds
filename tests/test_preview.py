"""Tests for the preview server (`thrds.preview.server`).

The server binds port 0 (ephemeral) and is exercised over real HTTP with
urllib — the same path the page takes.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from thrds.lint import DiscordLinter
from thrds.preview import DEFAULT_PORT
from thrds.preview.server import serve


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    p = tmp_path / 'draft.md'
    p.write_text('# Title\n\nHello.\n')
    return p


@pytest.fixture
def server(doc: Path):
    httpd = serve(doc, 0, lint=lambda text: DiscordLinter().lint(text).issues)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(url: str, obj: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_default_port_is_stable():
    # 3000 + crc32("thrds") % 1000 — a changed value would break every
    # bookmarked preview URL, so pin it.
    assert DEFAULT_PORT == 3077


def test_get_doc_returns_content_and_mtime(server: str, doc: Path):
    status, body = _get(f'{server}/doc')
    assert status == 200
    assert body == {
        'path': 'draft.md',
        'content': '# Title\n\nHello.\n',
        'mtime': str(doc.stat().st_mtime_ns),
    }
    # The token must be a JSON *string*: st_mtime_ns (~1.7e18) exceeds JS
    # Number.MAX_SAFE_INTEGER, so a numeric mtime is rounded by the page's
    # JSON.parse, never round-trips equal, and every save 409s. (Found live
    # in the first CIC pass.)
    assert type(body['mtime']) is str
    assert int(body['mtime']) > 2**53


def test_save_round_trip(server: str, doc: Path):
    _, before = _get(f'{server}/doc')
    status, body = _post(f'{server}/doc', {
        'content': '# Title v2\n\nHello again.\n',
        'base_mtime': before['mtime'],
    })
    assert status == 200
    assert body == {'mtime': str(doc.stat().st_mtime_ns)}
    assert doc.read_text() == '# Title v2\n\nHello again.\n'


def test_stale_base_is_refused_with_disk_content(server: str, doc: Path):
    _, before = _get(f'{server}/doc')
    # The file moves under the page (editor-side save).
    doc.write_text('# Edited in editor\n')
    status, body = _post(f'{server}/doc', {
        'content': '# Edited in page\n',
        'base_mtime': before['mtime'],
    })
    assert status == 409
    assert body == {
        'error': 'stale base',
        'path': 'draft.md',
        'content': '# Edited in editor\n',
        'mtime': str(doc.stat().st_mtime_ns),
    }
    # And the disk content was NOT clobbered by the losing write.
    assert doc.read_text() == '# Edited in editor\n'


def test_save_from_refreshed_base_succeeds_after_conflict(server: str, doc: Path):
    _, before = _get(f'{server}/doc')
    doc.write_text('# Edited in editor\n')
    status, conflict = _post(f'{server}/doc', {
        'content': '# Edited in page\n', 'base_mtime': before['mtime'],
    })
    assert status == 409
    # Retry from the base the conflict handed back — the merge path.
    status, body = _post(f'{server}/doc', {
        'content': '# Merged\n', 'base_mtime': conflict['mtime'],
    })
    assert status == 200
    assert doc.read_text() == '# Merged\n'


def test_lint_reflects_current_doc(server: str, doc: Path):
    doc.write_text('| a | b |\n|---|---|\n')
    status, body = _get(f'{server}/lint')
    assert status == 200
    assert body == {'issues': [
        {
            'line': 1, 'column': 1, 'severity': 'warning',
            'rule': 'discord/table',
            'message': "markdown table doesn't render in Discord; use a code block or bullets",
        },
        {
            'line': 2, 'column': 1, 'severity': 'warning',
            'rule': 'discord/table',
            'message': "markdown table doesn't render in Discord; use a code block or bullets",
        },
    ]}


def test_lint_clean_doc(server: str):
    status, body = _get(f'{server}/lint')
    assert status == 200
    assert body == {'issues': []}


def test_index_served_from_dist(server: str):
    with urllib.request.urlopen(f'{server}/') as r:
        assert r.status == 200
        assert r.headers['Content-Type'] == 'text/html; charset=utf-8'
        text = r.read().decode()
    # The page identifies itself by <title>; the renderer bundle that
    # replaces the stub keeps the same one.
    m = re.search(r'<title>(.*?)</title>', text)
    assert m is not None
    assert m.group(1) == 'thrds preview'


def test_path_traversal_refused(server: str):
    status, body = _get(f'{server}/../server.py')
    assert status == 404
    assert body == {'error': 'not found: /../server.py'}


def test_unknown_asset_404s(server: str):
    status, body = _get(f'{server}/nope.js')
    assert status == 404
    assert body == {'error': 'not found: /nope.js'}


def test_on_save_hook_fires_with_content(doc: Path):
    saved: list[str] = []
    httpd = serve(doc, 0, on_save=saved.append)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        _, before = _get(f'{base}/doc')
        status, _ = _post(f'{base}/doc', {'content': 'new\n', 'base_mtime': before['mtime']})
        assert status == 200
        assert saved == ['new\n']
    finally:
        httpd.shutdown()
