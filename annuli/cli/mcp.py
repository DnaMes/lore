#!/usr/bin/env python3
"""``lore-mcp`` entry point — run the Lore MCP server over stdio or streamable HTTP.

Stdio is the default and behaves exactly as before; ``--http`` starts the same
server (one shared ``create_server()`` factory) behind the streamable-HTTP
transport in :mod:`lore.interfaces.mcp_http`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from lore.interfaces.mcp import create_server

DEFAULT_HTTP_PORT = 8787
DEFAULT_HTTP_PATH = "/mcp"
DEFAULT_HTTP_TIMEOUT = 60.0


async def _serve_stdio() -> None:
    """Run the MCP server over stdio (the historical behaviour)."""
    server = create_server()
    await server.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lore-mcp",
        description=(
            "Run the Lore MCP server. It speaks JSON-RPC on stdin/stdout by default; "
            "--http serves the same tools over streamable HTTP instead."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve the streamable-HTTP transport instead of stdio.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LORE_MCP_HTTP_HOST", "127.0.0.1"),
        help=(
            "HTTP bind host (default 127.0.0.1, or $LORE_MCP_HTTP_HOST). A "
            "non-loopback host requires an auth token."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LORE_MCP_HTTP_PORT", str(DEFAULT_HTTP_PORT))),
        help="HTTP bind port (default 8787, or $LORE_MCP_HTTP_PORT).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("LORE_MCP_HTTP_PATH", DEFAULT_HTTP_PATH),
        help="HTTP endpoint path (default /mcp, or $LORE_MCP_HTTP_PATH).",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("LORE_MCP_HTTP_TOKEN_FILE") or None,
        help=(
            "File holding the bearer token (or set $LORE_MCP_HTTP_TOKEN). Required "
            "for a non-loopback bind; the token is read from the file, never from argv."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("LORE_MCP_HTTP_TIMEOUT", str(DEFAULT_HTTP_TIMEOUT))),
        help="Per-request deadline in seconds (default 60, or $LORE_MCP_HTTP_TIMEOUT).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if not args.http:
        asyncio.run(_serve_stdio())
        return 0

    from lore.interfaces.mcp_http import (
        MCPHTTPConfigError,
        resolve_http_token,
        run_http_server,
    )

    try:
        token = resolve_http_token(args.token_file)
        return run_http_server(
            create_server(),
            host=args.host,
            port=args.port,
            path=args.path,
            token=token,
            request_timeout=args.timeout,
        )
    except MCPHTTPConfigError as exc:
        print(f"lore-mcp: {exc}", file=sys.stderr)
        return 2


def main_sync() -> int:
    """Synchronous script wrapper for setuptools entry points."""
    return main()


if __name__ == "__main__":
    main_sync()
