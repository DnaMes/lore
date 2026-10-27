"""Streamable-HTTP transport for the Lore MCP server (issue #29).

Transport only: the tool surface stays in :mod:`lore.interfaces.mcp` /
:mod:`lore.interfaces.server`, and ``lore-mcp`` hands one shared
``create_server()`` result to either ``MCPServer.run()`` (stdio, the default) or
:func:`run_http_server`.

Protocol shape: one POST per JSON-RPC message to ``--path`` (default ``/mcp``)
carrying an ``application/json`` body, answered with ``application/json``. The
server is stateless — no ``Mcp-Session-Id``, no server-initiated SSE stream — so
a client may send ``initialize`` and every later request on separate
connections, and a notification is acknowledged with ``202 Accepted`` and an
empty body as the streamable-HTTP spec asks. A GET to the endpoint gets ``405``
because no standalone stream is offered.

The archive is private, so the transport is locked down by default:

- The bind host defaults to loopback. A non-loopback bind is an explicit opt-in
  and :func:`create_http_app` refuses to build without a bearer token
  (``LORE_MCP_HTTP_TOKEN`` or ``--token-file``).
- ``Host`` must pass the same allowlist as ``lore-web``
  (:func:`lore.interfaces.web_utils.host_header_allowed`), and a present
  ``Origin`` must name this server's own origin (or a configured public origin
  behind a trusted proxy) exactly as for the web UI. Foreign ``Host`` -> 400,
  cross-origin -> 403, following ``lore-web``'s status codes.
- Request bodies are bounded, in-flight requests are capped and every request
  has a wall-clock deadline: a timed-out tool call is cancelled and answered
  with JSON-RPC error ``-32000`` instead of hanging the client.
- The token is compared in constant time and is never logged or echoed.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request

from .web_utils import host_header_allowed, is_loopback_host, request_is_cross_origin

logger = logging.getLogger(__name__)

# Bearer token for a non-loopback bind (an explicit loopback bind with a token
# set also demands it — a token never weakens the transport).
MCP_HTTP_TOKEN_ENV = "LORE_MCP_HTTP_TOKEN"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_PATH = "/mcp"

# One MiB is far above any JSON-RPC tool call (the largest payload is a search
# query plus filters) and far below anything that could exhaust memory.
MAX_BODY_BYTES = 1_048_576
MAX_CONCURRENT_REQUESTS = 4
REQUEST_TIMEOUT_SECONDS = 60.0


class MCPHTTPConfigError(ValueError):
    """A configuration that must not start the transport (exposure, bad path)."""


def resolve_http_token(token_file: Optional[str] = None) -> Optional[str]:
    """Bearer token from ``--token-file`` (wins) or ``LORE_MCP_HTTP_TOKEN``.

    ``None`` means no token is configured, which only a loopback bind allows.
    The value is never logged; an unreadable or empty token file is a
    configuration error rather than a silently unauthenticated server.
    """
    if token_file:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MCPHTTPConfigError(
                f"cannot read token file {path}: {exc.strerror or exc}"
            ) from exc
        if not token:
            raise MCPHTTPConfigError(f"token file {path} is empty")
        return token
    token = (os.environ.get(MCP_HTTP_TOKEN_ENV) or "").strip()
    return token or None


def require_token_for_host(host: str, token: Optional[str]) -> None:
    """Refuse a non-loopback bind without a token; ``host`` is the bind host."""
    if token or is_loopback_host(host):
        return
    raise MCPHTTPConfigError(
        f"refusing to bind the MCP HTTP transport to non-loopback host {host!r} "
        f"without an auth token: set {MCP_HTTP_TOKEN_ENV} or pass --token-file "
        "(the archive is private)"
    )


def normalize_http_path(path: str) -> str:
    """Validate ``--path``: an absolute, single path segment-free route."""
    value = (path or "").strip()
    if not value.startswith("/") or value == "/" or any(c in value for c in " <>?#"):
        raise MCPHTTPConfigError(
            f"invalid HTTP path {path!r}: it must start with '/' and contain "
            "no spaces, query or fragment"
        )
    return value


def _bearer_token_matches(expected: str) -> bool:
    """Constant-time check of ``Authorization: Bearer <token>``."""
    scheme, _sep, value = (request.headers.get("Authorization") or "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), expected.encode("utf-8"))


def _rpc_error_response(request_id: object, code: int, message: str, status: int = 200) -> Response:
    """A JSON-RPC error envelope with an HTTP status chosen by the transport."""
    response = jsonify(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )
    response.status_code = status
    return response


async def _call_with_timeout(server, payload: dict, timeout: float):
    """Await one JSON-RPC request, cancelling it at ``timeout`` seconds."""
    return await asyncio.wait_for(server.handle_request(payload), timeout=timeout)


def _handle_post(server, request_timeout: float) -> Response:
    """Turn one HTTP POST into one JSON-RPC request/response round trip."""
    if not request.is_json:
        return _rpc_error_response(
            None, -32600, "Content-Type must be application/json", status=415
        )
    payload = request.get_json(silent=True)
    if payload is None:
        return _rpc_error_response(None, -32700, "Parse error: body is not valid JSON", status=400)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return _rpc_error_response(
            None, -32600, "Invalid Request: expected a JSON-RPC 2.0 request object", status=400
        )

    try:
        response = asyncio.run(_call_with_timeout(server, payload, request_timeout))
    except asyncio.TimeoutError:
        logger.warning(
            "MCP HTTP request timed out after %ss (method=%r)",
            request_timeout,
            str(payload.get("method"))[:60],
        )
        return _rpc_error_response(
            payload.get("id"),
            -32000,
            f"Request timed out after {request_timeout}s",
        )
    if response is None:
        # Notification (e.g. notifications/initialized): accepted, no body.
        return Response(status=202)
    return jsonify(response)


def create_http_app(
    server,
    *,
    path: str = DEFAULT_PATH,
    token: Optional[str] = None,
    bind_host: str = DEFAULT_HOST,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_concurrent: int = MAX_CONCURRENT_REQUESTS,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> Flask:
    """Build the streamable-HTTP WSGI app around one ``MCPServer``.

    Raises :class:`MCPHTTPConfigError` when the bind host is non-loopback and no
    token is given, so an accidental network exposure cannot start. The caller
    owns the process lifecycle (:func:`run_http_server`); tests drive the app
    directly.
    """
    require_token_for_host(bind_host, token)
    mcp_path = normalize_http_path(path)
    limiter = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = max_body_bytes
    app.config["MCP_HTTP_PATH"] = mcp_path
    app.config["MCP_HTTP_TOKEN_REQUIRED"] = bool(token)

    @app.before_request
    def guard_request() -> Optional[Response]:
        # Foreign Host first: a DNS-rebinding or poisoned Host never reaches a
        # route, exactly as in lore-web (#193).
        if not host_header_allowed(request.headers.get("Host", ""), bind_host):
            logger.warning(
                "Rejecting MCP HTTP request with disallowed Host header: %r",
                request.headers.get("Host", "")[:120],
            )
            return Response(
                "Bad Host header. Allowed hosts can be configured via LORE_ALLOWED_HOSTS.",
                status=400,
                mimetype="text/plain",
            )
        # A browser always sends Origin on cross-origin requests; MCP clients and
        # curl send none. Anything else is another site (or another local app).
        if request_is_cross_origin():
            logger.warning(
                "Rejecting cross-origin MCP HTTP request (Origin=%r)",
                (request.headers.get("Origin") or "")[:120],
            )
            return _rpc_error_response(None, -32600, "Cross-origin request rejected", status=403)
        if token and not _bearer_token_matches(token):
            logger.warning("Rejecting MCP HTTP request without a valid bearer token")
            response = _rpc_error_response(
                None, -32001, "Unauthorized: bearer token required", status=401
            )
            response.headers["WWW-Authenticate"] = 'Bearer realm="lore-mcp"'
            return response
        return None

    @app.route(mcp_path, methods=["POST"])
    def handle_mcp_post() -> Response:
        if not limiter.acquire(blocking=False):
            logger.warning(
                "Rejecting MCP HTTP request: %s request(s) already in flight", max_concurrent
            )
            response = _rpc_error_response(
                None, -32000, "Server busy: too many concurrent requests", status=503
            )
            response.headers["Retry-After"] = "1"
            return response
        try:
            return _handle_post(server, request_timeout)
        finally:
            limiter.release()

    @app.errorhandler(405)
    def method_not_allowed(_exc) -> Response:
        # No standalone SSE stream is offered, so GET/DELETE are not endpoints.
        response = _rpc_error_response(
            None, -32600, "Method not allowed: POST a JSON-RPC message to this endpoint", status=405
        )
        response.headers["Allow"] = "POST"
        return response

    @app.errorhandler(413)
    def body_too_large(_exc) -> Response:
        return _rpc_error_response(
            None, -32600, f"Request body exceeds {max_body_bytes} bytes", status=413
        )

    return app


def run_http_server(
    server,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    token: Optional[str] = None,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_concurrent: int = MAX_CONCURRENT_REQUESTS,
) -> int:
    """Serve ``server`` over streamable HTTP until interrupted; returns the exit code."""
    from werkzeug.serving import make_server

    app = create_http_app(
        server,
        path=path,
        token=token,
        bind_host=host,
        request_timeout=request_timeout,
        max_concurrent=max_concurrent,
    )
    try:
        httpd = make_server(host, port, app, threaded=True)
    except OSError as exc:
        raise MCPHTTPConfigError(f"cannot bind {host}:{port}: {exc.strerror or exc}") from exc

    auth = "bearer token required" if token else "none (loopback bind only)"
    print(
        f"Lore MCP server (streamable-http) on http://{host}:{httpd.server_port}"
        f"{app.config['MCP_HTTP_PATH']}",
        file=sys.stderr,
    )
    print(f"Auth: {auth}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
