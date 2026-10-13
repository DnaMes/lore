"""MCP shared-memory tools: ``memory_write`` and ``memory_recall`` (issue #44 / #33).

These tools turn lore into a cross-tool memory: an agent in one tool
records a fact, an agent in any other tool recalls it later. Backed by the v2
SQLite store's memory tables.
"""

from __future__ import annotations

from ...utils.security import (
    validate_search_param,
    validate_search_query,
    validate_session_id,
    validate_tool_name,
)
from ...utils.tooling import normalize_tool_name
from .deps import MCPToolDeps


def register(server, deps: MCPToolDeps) -> None:
    """Register the shared-memory tool group on ``server``."""

    async def memory_write(args: dict) -> str:
        from ...storage import MEMORY_KINDS, add_memory

        kind = str(args.get("kind") or "note").strip().lower()
        if kind not in MEMORY_KINDS:
            return f"Invalid kind. Expected one of: {', '.join(MEMORY_KINDS)}."
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            return "Both 'title' and 'body' are required and must be non-empty."

        scope_project = args.get("project") or None
        if scope_project and not validate_search_param(str(scope_project)):
            return "Invalid project parameter."
        scope_tool = normalize_tool_name(args.get("tool") or "") or None
        if scope_tool and not validate_tool_name(scope_tool):
            return "Invalid tool parameter."

        tags = args.get("tags")
        if tags is not None and not isinstance(tags, list):
            return "'tags' must be a list of strings."

        source_session = args.get("source_session") or None
        if source_session and not validate_session_id(str(source_session)):
            return "Invalid source_session parameter."

        try:
            memory_id = add_memory(
                deps.server.output_dir,
                kind=kind,
                title=title,
                body=body,
                author=str(args.get("author") or "agent"),
                scope_project=scope_project,
                scope_tool=scope_tool,
                tags=[str(t) for t in tags] if tags else None,
                source_session=str(source_session) if source_session else None,
            )
        except ValueError as exc:
            return f"Could not write memory: {exc}"
        return deps.json_text({"status": "stored", "memory_id": memory_id, "kind": kind})

    server.register_tool(
        "memory_write",
        "Record a durable memory (fact, decision, lesson, snippet, …) into the "
        "shared cross-tool knowledge store. Any AI tool can recall it later.",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "fact|decision|todo|snippet|link|lesson|note",
                },
                "title": {"type": "string"},
                "body": {"type": "string"},
                "project": {"type": "string", "description": "optional project scope"},
                "tool": {"type": "string", "description": "optional tool scope"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "author": {"type": "string"},
                "source_session": {
                    "type": "string",
                    "description": "optional id of the session this memory "
                    "came from — records the memory's provenance",
                },
            },
            "required": ["title", "body"],
        },
        memory_write,
    )

    async def memory_recall(args: dict) -> str:
        from ...storage import MEMORY_KINDS, recall_memory

        query = str(args.get("query") or "").strip()
        if query and not validate_search_query(query):
            return "Invalid search query."

        kind = args.get("kind")
        if kind is not None:
            kind = str(kind).strip().lower()
            if kind not in MEMORY_KINDS:
                return f"Invalid kind. Expected one of: {', '.join(MEMORY_KINDS)}."

        scope_project = args.get("project") or None
        if scope_project and not validate_search_param(str(scope_project)):
            return "Invalid project parameter."

        try:
            limit = deps.normalize_limit(args.get("limit"), default=10)
        except ValueError as exc:
            return str(exc)

        semantic = args.get("semantic")
        entries = recall_memory(
            deps.server.output_dir,
            query,
            kind=kind,
            scope_project=scope_project,
            include_superseded=bool(args.get("include_superseded")),
            limit=limit,
            semantic=None if semantic is None else bool(semantic),
        )
        return deps.json_text(
            {
                "count": len(entries),
                "memories": [e.to_dict() for e in entries],
            }
        )

    server.register_tool(
        "memory_recall",
        "Search the shared cross-tool memory store for facts/decisions/lessons "
        "recorded earlier — by you or by an agent in another tool. Matches "
        "keywords and, when the embedding backend is installed, meaning; each "
        "memory lists the sessions it came from. A query in double quotes is an "
        "exact phrase. Set semantic=false for keywords only.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query"},
                "kind": {"type": "string"},
                "project": {"type": "string"},
                "include_superseded": {"type": "boolean"},
                "semantic": {
                    "type": "boolean",
                    "description": "omit for auto (keywords plus meaning when the "
                    "'semantic' extra is installed); false = keywords only; "
                    "true = rank by embedding similarity, no relevance floor",
                },
                "limit": {"type": "integer"},
            },
        },
        memory_recall,
    )
