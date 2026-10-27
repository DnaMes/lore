"""Streamable-HTTP transport for the MCP server (issue #29).

The transport is exercised over a real socket on an ephemeral port: the tests
start the WSGI app in a background thread, talk JSON-RPC to it with
``http.client`` and check the security rails (Host allowlist, Origin policy,
bearer token, body and concurrency caps, request deadline) plus the fact that
stdio remains the default and unchanged.

There is no MCP SDK in this project — ``lore.interfaces.server.MCPServer`` is
the hand-written stdio implementation — so the HTTP transport is hand-written
too and these tests drive it directly instead of through an SDK test client.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from lore.cli import mcp as mcp_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.interfaces import mcp, mcp_http

REPO = Path(__file__).resolve().parent.parent
TOKEN = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def serve():
    """Start WSGI apps on ephemeral loopback ports; they are torn down at teardown."""
    started: list[tuple[object, threading.Thread]] = []

    def start(app) -> int:
        httpd = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return int(httpd.server_port)

    yield start

    for httpd, thread in started:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _seed_index(output_dir: Path) -> None:
    """One live session in a real index, so ``list_sessions`` has something to read."""
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="session-1",
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/demo",
        title="Claude debugging session",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="Find the sqlite issue",
                timestamp=datetime(2026, 4, 1, 10, 0, 0),
            )
        ],
    )
    IndexBuilder(output_dir).build_index([session], {})


@pytest.fixture()
def tool_server(tmp_path, monkeypatch):
    """A real MCP server whose index lives in a temporary output dir."""
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    _seed_index(tmp_path)
    return mcp.create_server()


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def _request(
    port: int,
    *,
    method: str = "POST",
    path: str = "/mcp",
    body: bytes = b"",
    headers: dict | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict, bytes]:
    """One HTTP request with full control over Host/Origin (``http.client`` does not rewrite them)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        merged = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        merged.update(headers or {})
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _rpc(port: int, payload: dict, **kwargs) -> tuple[int, dict, dict | None]:
    status, headers, raw = _request(port, body=json.dumps(payload).encode("utf-8"), **kwargs)
    return status, headers, (json.loads(raw) if raw else None)


# ---------------------------------------------------------------------------
# Happy path: initialize, tools/list, one read-only tool call
# ---------------------------------------------------------------------------


def test_initialize_tools_list_and_read_only_call_over_http(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server))

    status, headers, initialized = _rpc(port, INITIALIZE)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert initialized["result"]["serverInfo"]["name"] == "lore"
    assert initialized["result"]["capabilities"]["tools"] == {}

    # A notification is accepted with no body, as streamable HTTP asks.
    status, _headers, body = _rpc(port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert body is None

    status, _headers, listed = _rpc(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    assert "list_sessions" in {tool["name"] for tool in listed["result"]["tools"]}

    status, _headers, called = _rpc(
        port,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_sessions", "arguments": {"limit": 5}},
        },
    )
    assert status == 200
    payload = json.loads(called["result"]["content"][0]["text"])
    assert [entry["id"] for entry in payload["sessions"]] == ["session-1"]


# ---------------------------------------------------------------------------
# Security rails
# ---------------------------------------------------------------------------


def test_foreign_host_header_is_refused(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server))
    status, _headers, _body = _request(
        port,
        body=json.dumps(INITIALIZE).encode("utf-8"),
        headers={"Host": "evil.example.com"},
    )
    assert status == 400


def test_cross_origin_request_is_refused_and_own_origin_accepted(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server))

    status, _headers, _body = _rpc(port, INITIALIZE, headers={"Origin": "https://evil.example.com"})
    assert status == 403

    status, _headers, _body = _rpc(port, INITIALIZE, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200


def test_missing_and_incorrect_token_are_refused(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server, token=TOKEN))

    status, headers, body = _rpc(port, INITIALIZE)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Bearer")
    assert body["error"]["code"] == -32001

    status, _headers, _body = _rpc(
        port, INITIALIZE, headers={"Authorization": "Bearer not-the-token"}
    )
    assert status == 401

    status, _headers, initialized = _rpc(
        port, INITIALIZE, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200
    assert initialized["result"]["serverInfo"]["name"] == "lore"


def test_token_is_never_logged(tool_server, caplog):
    app = mcp_http.create_http_app(tool_server, token=TOKEN)
    client = app.test_client()
    with caplog.at_level("DEBUG"):
        assert (
            client.post(
                "/mcp", json=INITIALIZE, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/mcp", json=INITIALIZE, headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 200
        )
    assert TOKEN not in caplog.text


def test_non_loopback_bind_without_token_does_not_start(tool_server):
    with pytest.raises(mcp_http.MCPHTTPConfigError, match="non-loopback"):
        mcp_http.create_http_app(tool_server, bind_host="0.0.0.0", token=None)


def test_cli_refuses_non_loopback_bind_without_token(monkeypatch, capsys):
    monkeypatch.delenv("LORE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("LORE_MCP_HTTP_TOKEN_FILE", raising=False)

    def must_not_bind(*_args, **_kwargs):
        raise AssertionError("the HTTP transport must refuse to bind without a token")

    monkeypatch.setattr("werkzeug.serving.make_server", must_not_bind)
    assert mcp_cli.main(["--http", "--host", "0.0.0.0", "--port", "0"]) == 2
    assert "non-loopback" in capsys.readouterr().err


def test_cli_accepts_a_non_loopback_bind_with_a_token_file(monkeypatch, tmp_path):
    """The opt-in path: a token from a file gets past the refusal (the server itself is stubbed)."""
    token_file = tmp_path / "mcp-token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("LORE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("LORE_MCP_HTTP_TOKEN_FILE", raising=False)
    seen = {}

    def fake_run(server, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(mcp_http, "run_http_server", fake_run)
    rc = mcp_cli.main(["--http", "--host", "192.168.1.10", "--token-file", str(token_file)])
    assert rc == 0
    assert seen["token"] == TOKEN
    assert seen["host"] == "192.168.1.10"


def test_resolve_http_token_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("LORE_MCP_HTTP_TOKEN", raising=False)
    assert mcp_http.resolve_http_token() is None

    monkeypatch.setenv("LORE_MCP_HTTP_TOKEN", "  from-env  ")
    assert mcp_http.resolve_http_token() == "from-env"

    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    assert mcp_http.resolve_http_token(str(token_file)) == "from-file"

    empty = tmp_path / "empty-token"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(mcp_http.MCPHTTPConfigError, match="empty"):
        mcp_http.resolve_http_token(str(empty))

    with pytest.raises(mcp_http.MCPHTTPConfigError, match="cannot read token file"):
        mcp_http.resolve_http_token(str(tmp_path / "absent"))


def test_body_size_is_bounded(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server, max_body_bytes=64))
    oversized = (
        b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"pad": "'
        + (b"x" * 256)
        + b'"}}'
    )
    status, _headers, _body = _request(port, body=oversized)
    assert status == 413


def test_concurrent_requests_are_capped(serve):
    class BlockingServer:
        """Holds the first request open so the second one hits the cap."""

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        async def handle_request(self, payload):
            self.started.set()
            await asyncio.to_thread(self.release.wait, 10)
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}}

    stub = BlockingServer()
    port = serve(mcp_http.create_http_app(stub, max_concurrent=1, request_timeout=20))
    results: list[int] = []

    def first_request() -> None:
        status, _headers, _body = _rpc(port, INITIALIZE)
        results.append(status)

    worker = threading.Thread(target=first_request, daemon=True)
    worker.start()
    try:
        assert stub.started.wait(5)
        status, headers, body = _rpc(port, {**INITIALIZE, "id": 2})
        assert status == 503
        assert headers["Retry-After"] == "1"
        assert body["error"]["code"] == -32000
    finally:
        stub.release.set()
        worker.join(timeout=10)
    assert results == [200]


def test_request_deadline_cancels_a_hanging_tool(serve):
    class SlowServer:
        async def handle_request(self, payload):
            await asyncio.sleep(30)
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}

    port = serve(mcp_http.create_http_app(SlowServer(), request_timeout=0.2))
    started = time.monotonic()
    status, _headers, body = _rpc(port, INITIALIZE)
    elapsed = time.monotonic() - started
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "timed out" in body["error"]["message"]
    assert elapsed < 10


def test_get_and_malformed_bodies_are_rejected(serve, tool_server):
    port = serve(mcp_http.create_http_app(tool_server))

    status, headers, _body = _request(port, method="GET", headers={"Accept": "text/event-stream"})
    assert status == 405
    assert headers["Allow"] == "POST"

    status, _headers, raw = _request(port, body=b"{not json")
    assert status == 400
    assert json.loads(raw)["error"]["code"] == -32700

    status, _headers, body = _request(port, body=b"hello", headers={"Content-Type": "text/plain"})
    assert status == 415


# ---------------------------------------------------------------------------
# stdio stays the default and unchanged
# ---------------------------------------------------------------------------


def test_parser_defaults_keep_stdio_and_describe_http(monkeypatch):
    for name in (
        "LORE_MCP_HTTP_HOST",
        "LORE_MCP_HTTP_PORT",
        "LORE_MCP_HTTP_PATH",
        "LORE_MCP_HTTP_TOKEN_FILE",
        "LORE_MCP_HTTP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    args = mcp_cli.build_parser().parse_args([])
    assert args.http is False
    assert args.host == "127.0.0.1"
    assert args.port == 8787
    assert args.path == "/mcp"
    assert args.timeout == 60.0

    args = mcp_cli.build_parser().parse_args(
        ["--http", "--host", "127.0.0.1", "--port", "9137", "--path", "/mcp", "--timeout", "5"]
    )
    assert args.http is True
    assert args.port == 9137
    assert args.timeout == 5.0


def test_stdio_transport_is_unchanged():
    """With no flags the entry point still speaks JSON-RPC on stdin/stdout."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from lore.cli.mcp import main_sync; raise SystemExit(main_sync())",
        ],
        cwd=REPO,
        input=json.dumps(INITIALIZE).encode("utf-8") + b"\n",
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    response = json.loads(lines[-1])
    assert response["result"]["serverInfo"]["name"] == "lore"
