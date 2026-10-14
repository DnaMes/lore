"""MCP tool handler groups.

``create_server()`` in :mod:`lore.interfaces.mcp` stays a thin
assembler: it builds the :class:`~lore.interfaces.mcp_tools.deps.MCPToolDeps`
bundle of shared helpers and calls each group module's ``register(server, deps)``.

Each module exposes a ``register`` function that registers its tools with an
unchanged name, description, input schema and handler logic.
"""

from __future__ import annotations

from .deps import MCPToolDeps, build_deps
from .memory import register as register_memory
from .search import register as register_search
from .sessions import register as register_sessions
from .summaries import register as register_summaries

__all__ = [
    "MCPToolDeps",
    "build_deps",
    "register_memory",
    "register_search",
    "register_sessions",
    "register_summaries",
]
