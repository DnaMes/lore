"""Shared dependencies passed to every MCP tool group.

The MCP tool handlers historically closed over a handful of helpers defined
inside ``create_server`` (``_json_text``, ``_ensure_index``,
``_normalize_limit``, ``_load_live_session``, ``_session_meta_by_id``) plus the
index-access functions ``load_index`` / ``load_sessions_for_tool`` /
``search_index``.

Splitting the handlers into group modules means those helpers must be passed in
explicitly. :class:`MCPToolDeps` is that explicit bundle. :func:`build_deps`
constructs it, capturing the index-access functions from the ``mcp`` module at
``create_server()`` time so test ``monkeypatch.setattr(mcp, "load_index", ...)``
calls (which always run before ``create_server()``) are honoured. The helper
behaviour is unchanged from the original closures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ...services import build_index_if_missing as _service_build_index_if_missing
from ...services.index import session_by_id_map
from ..server import MCPServer


def _json_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _normalize_limit(raw_value: object, default: int = 10, max_value: int = 100) -> int:
    if raw_value is None:
        return default
    if not isinstance(raw_value, int) or raw_value < 1 or raw_value > max_value:
        raise ValueError(f"Invalid limit parameter (must be 1-{max_value}).")
    return raw_value


@dataclass
class MCPToolDeps:
    """Explicit bundle of helpers shared across MCP tool group modules."""

    server: MCPServer
    load_index: Callable[[], dict]
    load_sessions_for_tool: Callable[..., list]
    search_index: Callable[..., list]
    find_live_session: Callable[..., Optional[Any]]
    index_path: object

    # -- helpers (formerly closures inside create_server) -------------------

    @staticmethod
    def json_text(payload: dict) -> str:
        return _json_text(payload)

    @staticmethod
    def normalize_limit(raw_value: object, default: int = 10, max_value: int = 100) -> int:
        return _normalize_limit(raw_value, default=default, max_value=max_value)

    def ensure_index(self) -> dict:
        _service_build_index_if_missing(self.server.output_dir, self.index_path)
        return self.load_index()

    def load_live_session(self, session_id: str, tool_filter: Optional[str] = None):
        tools: list[str] = []
        if tool_filter:
            tools.append(tool_filter)
        tools.extend(
            [
                tool_name
                for tool_name in sorted(
                    {
                        session.get("tool")
                        for session in self.ensure_index().get("sessions", [])
                        if session.get("tool")
                    }
                )
                if tool_name not in tools
            ]
        )
        for tool_name in tools:
            # Direct single-file lookup first (#127): a cold cache used to
            # pay the full per-tool extraction just to find one session.
            # Falls through to the cached bulk scan when the tool has no
            # deterministic layout or the id is not directly addressable.
            try:
                direct = self.find_live_session(session_id, tool_name)
            except Exception:
                direct = None
            if direct is not None:
                return direct
            try:
                sessions = self.load_sessions_for_tool(tool_name)
            except Exception:
                continue
            for session in sessions:
                if session.session_id == session_id:
                    return session
        return None

    def session_meta_by_id(self, session_id: str) -> Optional[dict]:
        idx = self.ensure_index()
        # O(1) dict hit via the shared payload-identity memo (#126) instead
        # of a linear scan over every indexed session.
        return session_by_id_map(idx).get(session_id)


def build_deps(server: MCPServer, mcp_module) -> MCPToolDeps:
    """Assemble :class:`MCPToolDeps`, capturing index-access functions from ``mcp``.

    ``mcp_module`` is passed in (rather than imported) so the bundle picks up
    whatever ``load_index`` / ``load_sessions_for_tool`` / ``search_index`` are
    bound on the ``mcp`` module at the moment ``create_server()`` runs — which
    is exactly what monkeypatched tests rely on.
    """
    return MCPToolDeps(
        server=server,
        load_index=mcp_module.load_index,
        load_sessions_for_tool=mcp_module.load_sessions_for_tool,
        search_index=mcp_module.search_index,
        find_live_session=getattr(mcp_module, "find_live_session_by_id", None)
        or _fallback_find_live_session,
        index_path=mcp_module.INDEX_PATH,
    )


def _fallback_find_live_session(session_id, tool=None):
    """Direct-lookup shim for ``mcp`` modules predating #127 wiring."""
    from ...services import find_live_session

    return find_live_session(session_id, tool)
