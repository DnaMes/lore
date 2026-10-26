"""lore-web must not wait for a reverse DNS lookup when it binds its socket.

On GitHub's macOS runners the first request took ~35 s because HTTPServer.server_bind calls
socket.getfqdn(); Linux and Windows answered in about a second.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest
from werkzeug import serving

from lore.interfaces.fast_bind import skip_reverse_dns_on_bind


def test_bind_makes_no_reverse_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    original = serving.BaseWSGIServer.server_bind
    monkeypatch.setattr(serving.BaseWSGIServer, "server_bind", original)  # restored after the test

    def slow(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("socket.getfqdn() was called while binding")

    monkeypatch.setattr(socket, "getfqdn", slow)
    skip_reverse_dns_on_bind()
    skip_reverse_dns_on_bind()  # idempotent

    def app(environ, start_response):  # minimal WSGI app
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    server = serving.make_server("127.0.0.1", 0, app)
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1]
    finally:
        server.server_close()


def test_patched_server_still_serves_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    monkeypatch.setattr(serving.BaseWSGIServer, "server_bind", serving.BaseWSGIServer.server_bind)
    skip_reverse_dns_on_bind()

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [environ["SERVER_NAME"].encode()]

    server = serving.make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:  # noqa: S310
            assert response.read() == b"127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
