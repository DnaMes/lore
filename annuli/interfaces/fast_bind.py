"""Start the dev server without a reverse DNS lookup.

``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` to fill ``server_name``.
On macOS (and any machine whose hostname does not resolve) that lookup stalls for tens of
seconds, so ``lore-web`` took ~35 s to answer its first request on GitHub's macOS runners,
against 1 s on Linux and Windows. Nothing in Lore reads ``server_name``: Werkzeug builds
``SERVER_NAME`` from the bound address.
"""

from __future__ import annotations

import socketserver

from werkzeug import serving


def _server_bind_without_lookup(self: serving.BaseWSGIServer) -> None:
    socketserver.TCPServer.server_bind(self)
    host, port = self.server_address[:2]
    self.server_name = str(host)
    self.server_port = port


def skip_reverse_dns_on_bind() -> None:
    """Patch Werkzeug's server class in this process; safe to call more than once."""
    serving.BaseWSGIServer.server_bind = _server_bind_without_lookup  # type: ignore[method-assign]
