"""HTTP plumbing for `thrds <platform> preview`.

Endpoints:

- ``GET /``, ``GET /<asset>`` — static files from the vendored ``dist/``.
- ``GET /doc`` — ``{"path", "content", "mtime"}``; ``mtime`` is the file's
  st_mtime_ns, the page's write-back base.
- ``POST /doc`` — body ``{"content", "base_mtime"}``. Refused with 409 (and
  the current doc) when ``base_mtime`` no longer matches the file on disk:
  the write must be based on what the file *is*, not on what the page last
  saw. Same invariant as the push gate (`specs/done/push-gate-ancestry.md`),
  one layer down.
- ``GET /lint`` — the platform linter's issues for the doc as it is now.

No tokens, no network beyond localhost, no non-stdlib dependencies.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

# 3000 + crc32("thrds") % 1000 — stable, documented, unlikely-collision
# default per the per-project port convention.
DEFAULT_PORT = 3000 + zlib.crc32(b'thrds') % 1000
assert DEFAULT_PORT == 3077

DIST_DIR = Path(__file__).parent / 'dist'

_CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
}


def _mtime_token(doc_path: Path) -> str:
    # st_mtime_ns as a *string*: ~1.7e18 exceeds JS Number.MAX_SAFE_INTEGER,
    # so a numeric mtime gets silently rounded by the page's JSON.parse and
    # never round-trips equal — every save would 409. Opaque token; the page
    # only ever compares and echoes it.
    return str(doc_path.stat().st_mtime_ns)


def _read_doc(doc_path: Path) -> dict:
    return {
        'path': doc_path.name,
        'content': doc_path.read_text(),
        'mtime': _mtime_token(doc_path),
    }


def make_handler(
    doc_path: Path,
    lint: Callable[[str], list] | None = None,
    on_save: Callable[[str], None] | None = None,
    dist_dir: Path = DIST_DIR,
) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class over a concrete doc path.

    ``lint(text) -> list[LintIssue]`` supplies the platform linter;
    ``on_save(content)`` runs after a successful write-back (e.g. a session
    commit when ``preview -c``).
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass  # keep the terminal quiet; the CLI prints the URL once

        def _json(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split('?', 1)[0]
            if path == '/doc':
                self._json(200, _read_doc(doc_path))
            elif path == '/lint':
                issues = lint(doc_path.read_text()) if lint else []
                self._json(200, {'issues': [asdict(i) for i in issues]})
            else:
                self._static(path)

        def _static(self, path: str) -> None:
            name = path.lstrip('/') or 'index.html'
            file = (dist_dir / name).resolve()
            # Refuse traversal out of dist/ and anything not present.
            if not file.is_relative_to(dist_dir.resolve()) or not file.is_file():
                self._json(404, {'error': f'not found: {path}'})
                return
            body = file.read_bytes()
            self.send_response(200)
            ctype = _CONTENT_TYPES.get(file.suffix, 'application/octet-stream')
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path.split('?', 1)[0] != '/doc':
                self._json(404, {'error': f'not found: {self.path}'})
                return
            length = int(self.headers.get('Content-Length', 0))
            try:
                req = json.loads(self.rfile.read(length))
                content = req['content']
                base_mtime = req['base_mtime']
            except (json.JSONDecodeError, KeyError) as e:
                self._json(400, {'error': f'bad request: {e}'})
                return
            current = _mtime_token(doc_path)
            if base_mtime != current:
                # The file moved under the page (editor save, another tab, a
                # pull). Hand back what's on disk; the page merges and
                # retries from the new base.
                self._json(409, {'error': 'stale base', **_read_doc(doc_path)})
                return
            doc_path.write_text(content)
            if on_save is not None:
                on_save(content)
            self._json(200, {'mtime': _mtime_token(doc_path)})

    return Handler


def serve(
    doc_path: Path,
    port: int,
    lint: Callable[[str], list] | None = None,
    on_save: Callable[[str], None] | None = None,
    dist_dir: Path = DIST_DIR,
) -> ThreadingHTTPServer:
    """Bind and return the server (caller runs ``serve_forever``; port 0 picks
    an ephemeral port — tests use this)."""
    handler = make_handler(doc_path, lint=lint, on_save=on_save, dist_dir=dist_dir)
    return ThreadingHTTPServer(('127.0.0.1', port), handler)
