"""MCP search tools: ``search_history`` and ``list_sessions``."""

from __future__ import annotations

from datetime import datetime

from ...services.index import (
    InvalidFlagError,
    automated_hidden_line,
    is_automated,
    parse_bool_flag,
)
from ...utils.datetime import make_naive, parse_duration
from ...utils.security import (
    validate_search_param,
    validate_search_query,
    validate_session_id,
    validate_tool_name,
)
from ...utils.tooling import normalize_tool_name
from ..api_payloads import serialize_index_session_summary, serialize_search_result
from .deps import MCPToolDeps


def register(server, deps: MCPToolDeps) -> None:
    """Register the search tool group on ``server``."""

    async def search_history(args: dict) -> str:
        from ...storage.search import SEARCH_SCOPES

        query = args.get("query", "")
        tool_filter = normalize_tool_name(args.get("tool") or "") or None
        project_filter = args.get("project") or None
        limit = deps.normalize_limit(args.get("limit"), default=10)
        scope = str(args.get("scope") or "all").strip().lower()
        try:
            include_automated = parse_bool_flag(
                args.get("include_automated"), name="include_automated"
            )
        except InvalidFlagError as exc:
            return str(exc)

        if not query or not validate_search_query(query):
            return "Invalid search query parameter."
        if tool_filter and not validate_tool_name(tool_filter):
            return "Invalid tool parameter."
        if project_filter and not validate_search_param(project_filter):
            return "Invalid project parameter."
        if scope not in SEARCH_SCOPES:
            return f"Invalid scope parameter. Expected one of: {', '.join(sorted(SEARCH_SCOPES))}."

        deps.ensure_index()
        # Only passed when set: the default (hide) is the callee's own default.
        extra = {"include_automated": True} if include_automated else {}
        try:
            results = deps.search_index(
                query,
                tool=tool_filter,
                project=project_filter,
                limit=limit,
                scope=scope,
                **extra,
            )
        except ValueError as exc:
            return str(exc)
        payload = {
            "query": query,
            "tool": tool_filter,
            "project": project_filter,
            "scope": scope,
            "count": len(results),
            "results": [serialize_search_result(result) for result in results],
        }
        return deps.json_text(payload)

    server.register_tool(
        "search_history",
        "Search across all AI chat sessions. Use 'scope' to limit matches to "
        "a message-role subset: user_only, assistant_only or tool_results.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tool": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer"},
                "scope": {
                    "type": "string",
                    "enum": ["all", "user_only", "assistant_only", "tool_results"],
                    "description": "Restrict matches to a message-role subset "
                    "(default 'all' searches whole sessions).",
                },
                "include_automated": {
                    "type": "boolean",
                    "description": "Also search exec/cron/subagent sessions (hidden by default).",
                },
            },
            "required": ["query"],
        },
        search_history,
    )

    async def list_sessions(args: dict) -> str:
        tool_filter = normalize_tool_name(args.get("tool") or "") or None
        project_filter = args.get("project") or None
        thread_filter = args.get("thread_id") or None
        since = args.get("since")
        limit = deps.normalize_limit(args.get("limit"), default=20)
        try:
            include_automated = parse_bool_flag(
                args.get("include_automated"), name="include_automated"
            )
        except InvalidFlagError as exc:
            return str(exc)

        if tool_filter and not validate_tool_name(tool_filter):
            return "Invalid tool parameter."
        if project_filter and not validate_search_param(project_filter):
            return "Invalid project parameter."
        if thread_filter and not validate_session_id(thread_filter):
            return "Invalid thread_id parameter."

        sessions = []
        cutoff = None
        if since:
            try:
                cutoff = datetime.now() - parse_duration(since)
            except ValueError:
                return "Invalid since parameter."

        hidden = 0
        for session in deps.ensure_index().get("sessions", []):
            if tool_filter and session.get("tool") != tool_filter:
                continue
            if project_filter and project_filter not in str(session.get("project") or ""):
                continue
            if thread_filter and session.get("thread_id") != thread_filter:
                continue
            if cutoff:
                created = session.get("created")
                try:
                    created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                except ValueError:
                    created_dt = None
                if created_dt and make_naive(created_dt) < cutoff:
                    continue
            # Counted after the filters above: a scoped query reports its own number.
            if not include_automated and is_automated(session):
                hidden += 1
                continue
            sessions.append(session)

        sessions.sort(key=lambda s: str(s.get("updated") or s.get("created") or ""), reverse=True)
        payload = {
            "count": min(len(sessions), limit),
            "sessions": [serialize_index_session_summary(session) for session in sessions[:limit]],
        }
        if hidden:
            payload["automated_hidden"] = hidden
            payload["automated_hidden_line"] = automated_hidden_line(hidden)
        return deps.json_text(payload)

    server.register_tool(
        "list_sessions",
        "List AI chat sessions with optional filters.",
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "project": {"type": "string"},
                "thread_id": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
                "include_automated": {
                    "type": "boolean",
                    "description": "Also list exec/cron/subagent sessions (hidden by default).",
                },
            },
        },
        list_sessions,
    )
