"""lore MCP server.

This module stays the public surface for the MCP server: the ``lore-mcp``
entry point and the test suite both import it. The stdio JSON-RPC machinery
lives in :mod:`lore.interfaces.server` (``MCPServer``) and the tool
handlers are grouped under :mod:`lore.interfaces.mcp_tools`.

``create_server()`` is a thin assembler: it builds the ``MCPServer``, wires the
shared :class:`~lore.interfaces.mcp_tools.deps.MCPToolDeps` bundle, and
calls each group's ``register(server, deps)``.

The index-access functions (``load_index``, ``load_sessions_for_tool``,
``search_index``) and ``INDEX_PATH`` stay defined here as module globals: they
are the historical ``monkeypatch.setattr(mcp, ...)`` targets and must remain so.
"""

import logging

logger = logging.getLogger(__name__)

from ..services import (
    collect_sessions,
)
from ..services import (
    find_live_session as _service_find_live_session,
)
from ..services import (
    load_deleted_session_ids as _service_load_deleted_session_ids,
)
from ..services import (
    load_index as _service_load_index,
)
from ..services import (
    search_index as _service_search_index,
)
from ..utils.paths import lore_home
from .mcp_tools import build_deps, register_memory, register_search, register_sessions
from .server import MCPServer

__all__ = [
    "MCPServer",
    "OUTPUT_DIR",
    "INDEX_PATH",
    "load_index",
    "load_sessions_for_tool",
    "find_live_session_by_id",
    "search_index",
    "create_server",
]

# Default output directory for the MCP server. Kept as a module global so
# tests can monkeypatch ``mcp.INDEX_PATH`` (the historical patch target).
OUTPUT_DIR = lore_home()
INDEX_PATH = OUTPUT_DIR / "index.json"


def _deleted_ids() -> set:
    """Tombstone set, resolved relative to the (patchable) INDEX_PATH."""
    return _service_load_deleted_session_ids(INDEX_PATH.parent / "deleted_sessions.json")


def load_index():
    """Load the search index from the MCP server's output directory."""
    return _service_load_index(INDEX_PATH, INDEX_PATH.parent, _deleted_ids())


def load_sessions_for_tool(tool=None):
    """Collect live sessions for a tool (shared service-layer routine)."""
    return collect_sessions(tool, deleted_ids=_deleted_ids())


def find_live_session_by_id(session_id, tool=None):
    """Direct single-session lookup honouring tombstones (#127).

    Parses only the matching source file when the tool's layout allows it
    (see ``BaseExtractor.find_session_by_id``); returns None otherwise so
    callers fall back to the cached bulk scan.
    """
    return _service_find_live_session(session_id, tool, deleted_ids=_deleted_ids())


def search_index(query, tool=None, project=None, limit=50, scope=None):
    """Search the index from the MCP server's output directory."""
    return _service_search_index(
        INDEX_PATH,
        query,
        _deleted_ids(),
        tool=tool,
        project=project,
        limit=limit,
        scope=scope,
    )


def create_server() -> MCPServer:
    """Create and configure the MCP server."""
    server = MCPServer()
    server.output_dir = INDEX_PATH.parent

    # Build the shared helper bundle, then let each tool group register itself.
    # ``build_deps`` captures load_index / load_sessions_for_tool / search_index
    # / INDEX_PATH off this module, so monkeypatched tests are honoured.
    import sys

    deps = build_deps(server, sys.modules[__name__])
    register_search(server, deps)
    register_sessions(server, deps)
    register_memory(server, deps)
    return server
