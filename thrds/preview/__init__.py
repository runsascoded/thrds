"""Local preview server for platform drafts.

Serves a session's doc at ``/doc`` (GET/POST, with a staleness check on
write-back) beside a static UI bundle from ``dist/``. The bundle is a
prebuilt artifact vendored as package data — see
``specs/discord-preview.md`` — so thrds stays ``pip install``-able with no
node toolchain. Until the Discord-faithful renderer bundle lands (built in
``discord-agent``, per its ``specs/discord-md-parser.md``), ``dist/``
holds a stub UI: source editing + write-back + live lint, with the render
pane explicitly labeled as pending.
"""

from .server import DEFAULT_PORT, serve

__all__ = ['DEFAULT_PORT', 'serve']
