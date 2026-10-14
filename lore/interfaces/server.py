"""Stdio JSON-RPC MCP server implementation.

``MCPServer`` was extracted from :mod:`lore.interfaces.mcp` so the
``mcp_tools`` group modules can import it without a circular dependency. The
class is unchanged; :mod:`lore.interfaces.mcp` re-exports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Optional

from .. import __version__
from ..utils.paths import lore_home

logger = logging.getLogger(__name__)


class MCPServer:
    """Simple MCP Server implementation using stdio."""

    def __init__(self):
        self.tools = {}
        self.output_dir = lore_home()

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler,
        annotations: Optional[dict] = None,
    ):
        """Register a tool with the server."""
        entry = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
        }
        if annotations:
            entry["annotations"] = annotations
        self.tools[name] = entry

    async def handle_request(self, request: dict) -> Optional[dict]:
        """Handle an incoming JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        if method == "initialize":
            return self._handle_initialize(request_id, params)
        elif method == "tools/list":
            return self._handle_tools_list(request_id)
        elif method == "tools/call":
            return await self._handle_tools_call(request_id, params)
        elif method == "notifications/initialized":
            return None  # No response for notifications
        else:
            return self._error_response(request_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, request_id, params) -> dict:
        """Handle initialize request."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "lore",
                    "version": __version__,
                },
            },
        }

    def _handle_tools_list(self, request_id) -> dict:
        """Handle tools/list request."""
        tools = []
        for tool in self.tools.values():
            listed = {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
            if tool.get("annotations"):
                listed["annotations"] = tool["annotations"]
            tools.append(listed)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tools},
        }

    async def _handle_tools_call(self, request_id, params) -> dict:
        """Handle tools/call request."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name not in self.tools:
            return self._error_response(request_id, -32602, f"Unknown tool: {tool_name}")

        try:
            handler = self.tools[tool_name]["handler"]
            result = await handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result}],
                },
            }
        except Exception:
            logger.exception("Tool %s raised an unexpected error", tool_name)
            return self._error_response(request_id, -32603, "Internal error")

    def _error_response(self, request_id, code: int, message: str) -> dict:
        """Create an error response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def run(self):
        """Run the server, reading from stdin and writing to stdout."""

        def read_exactly(num_bytes: int) -> bytes:
            data = b""
            while len(data) < num_bytes:
                chunk = sys.stdin.buffer.read(num_bytes - len(data))
                if not chunk:
                    return b""
                data += chunk
            return data

        def write_response(message: dict, use_content_length: bool) -> None:
            if use_content_length:
                payload = json.dumps(message).encode("utf-8")
                header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
                sys.stdout.buffer.write(header + payload)
                sys.stdout.buffer.flush()
            else:
                sys.stdout.write(json.dumps(message) + "\n")
                sys.stdout.flush()

        while True:
            use_content_length = False
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.buffer.readline
                )
                if not line:
                    break

                if line.strip() == b"":
                    continue

                if line.lower().startswith(b"content-length:"):
                    use_content_length = True
                    headers = {}
                    header_line = line.decode("utf-8", "replace").strip()
                    key, _, value = header_line.partition(":")
                    headers[key.lower()] = value.strip()

                    while True:
                        next_line = await asyncio.get_event_loop().run_in_executor(
                            None, sys.stdin.buffer.readline
                        )
                        if not next_line:
                            return
                        if next_line in (b"\r\n", b"\n", b""):
                            break
                        header_text = next_line.decode("utf-8", "replace").strip()
                        if not header_text:
                            break
                        h_key, _, h_value = header_text.partition(":")
                        headers[h_key.lower()] = h_value.strip()

                    content_length = int(headers.get("content-length", "0"))
                    if content_length <= 0:
                        continue

                    body = await asyncio.get_event_loop().run_in_executor(
                        None, read_exactly, content_length
                    )
                    if not body:
                        break
                    request = json.loads(body.decode("utf-8"))
                else:
                    request = json.loads(line.decode("utf-8").strip())
                response = await self.handle_request(request)

                if response is not None:
                    write_response(response, use_content_length)

            except json.JSONDecodeError as e:
                error = self._error_response(None, -32700, f"Parse error: {e}")
                write_response(error, use_content_length)
            except Exception:
                logger.exception("Unhandled error in MCP request loop")
                error = self._error_response(None, -32603, "Internal error")
                write_response(error, use_content_length)
