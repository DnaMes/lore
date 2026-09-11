"""MCP session tools: get_session, get_session_messages, list_recent_sessions,
list_projects, get_thread and switch_to_tool."""

from __future__ import annotations

import json
import subprocess

from ...utils.security import validate_session_id, validate_tool_name
from ...utils.tooling import normalize_tool_name, to_session_switch_tool
from ..api_payloads import (
    serialize_index_session_summary,
    serialize_live_session,
    serialize_project,
    serialize_thread_messages,
    serialize_thread_overview,
)
from ..web_services import (
    build_projects_payload,
    build_thread_detail_payload,
    thread_overview_by_id,
)
from .deps import MCPToolDeps


def register(server, deps: MCPToolDeps) -> None:
    """Register the session tool group on ``server``."""

    async def get_session(args: dict) -> str:
        session_id = args.get("session_id")
        include_messages = bool(args.get("include_messages", False))

        if not session_id or not validate_session_id(session_id):
            return "Invalid session_id parameter."

        session_meta = deps.session_meta_by_id(session_id)
        if not session_meta:
            return deps.json_text({"error": "Session not found", "session_id": session_id})

        live_session = deps.load_live_session(session_id, session_meta.get("tool"))
        if live_session:
            payload = serialize_live_session(
                live_session,
                session_meta=session_meta,
                include_messages=include_messages,
            )
        else:
            payload = serialize_index_session_summary(session_meta)
            payload["live"] = False
            if include_messages:
                payload["messages"] = []
        # Surface user-editable tags / bookmarks (#56).
        from ...storage import get_session_tags

        try:
            payload["user_tags"] = get_session_tags(deps.server.output_dir, session_id)
        except Exception:  # noqa: BLE001 - tags are best-effort, never fail the read
            payload["user_tags"] = []
        return deps.json_text(payload)

    server.register_tool(
        "get_session",
        "Get a single session by id.",
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "include_messages": {"type": "boolean"},
            },
            "required": ["session_id"],
        },
        get_session,
    )

    async def get_session_messages(args: dict) -> str:
        session_id = args.get("session_id")
        limit = deps.normalize_limit(args.get("limit"), default=200, max_value=1000)

        if not session_id or not validate_session_id(session_id):
            return "Invalid session_id parameter."

        session_meta = deps.session_meta_by_id(session_id)
        if not session_meta:
            return deps.json_text({"error": "Session not found", "session_id": session_id})

        live_session = deps.load_live_session(session_id, session_meta.get("tool"))
        if not live_session:
            return deps.json_text(
                {
                    "session_id": session_id,
                    "message_count": 0,
                    "messages": [],
                    "live": False,
                }
            )

        messages = serialize_live_session(
            live_session,
            session_meta=session_meta,
            include_messages=True,
        )["messages"]
        return deps.json_text(
            {
                "session_id": session_id,
                "message_count": len(messages),
                "messages": messages[:limit],
                "live": True,
            }
        )

    server.register_tool(
        "get_session_messages",
        "Get live session messages by session id.",
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["session_id"],
        },
        get_session_messages,
    )

    async def list_recent_sessions(args: dict) -> str:
        limit = deps.normalize_limit(args.get("limit"), default=10)
        sessions = sorted(
            deps.ensure_index().get("sessions", []),
            key=lambda session: str(session.get("updated") or session.get("created") or ""),
            reverse=True,
        )
        return deps.json_text(
            {
                "count": min(len(sessions), limit),
                "sessions": [
                    serialize_index_session_summary(session) for session in sessions[:limit]
                ],
            }
        )

    server.register_tool(
        "list_recent_sessions",
        "List recently updated sessions.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
            },
        },
        list_recent_sessions,
    )

    async def list_projects(args: dict) -> str:
        limit = deps.normalize_limit(args.get("limit"), default=50)
        projects = build_projects_payload(
            deps.ensure_index().get("sessions", []), lambda value: value
        )
        return deps.json_text(
            {
                "count": min(len(projects), limit),
                "projects": [serialize_project(project) for project in projects[:limit]],
            }
        )

    server.register_tool(
        "list_projects",
        "List projects present in the history index.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
            },
        },
        list_projects,
    )

    async def get_thread(args: dict) -> str:
        thread_id = args.get("thread_id")
        include_messages = bool(args.get("include_messages", False))

        if not thread_id or not validate_session_id(thread_id):
            return "Invalid thread_id parameter."

        idx = deps.ensure_index()
        # O(1) bucket hit via the memoized {thread_id: overview} map (#126)
        # instead of regrouping every indexed session per call.
        overview = thread_overview_by_id(idx.get("sessions", [])).get(thread_id)
        if not overview:
            return deps.json_text({"error": "Thread not found", "thread_id": thread_id})

        payload: dict[str, object] = {"thread": serialize_thread_overview(overview)}
        if include_messages:
            detail = build_thread_detail_payload(
                thread_id,
                idx.get("sessions", []),
                deps.load_sessions_for_tool,
                lambda value: value,
                lambda value: json.dumps(value, ensure_ascii=False),
                lambda tool_name: {"tool": tool_name, "name": tool_name},
            )
            payload["messages"] = serialize_thread_messages(detail["messages"])
            payload["thread_meta"] = detail["thread_meta"]
            payload["timeline"] = [
                serialize_index_session_summary(session) for session in detail["thread_timeline"]
            ]
        return deps.json_text(payload)

    server.register_tool(
        "get_thread",
        "Get a thread overview and optional messages.",
        {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "include_messages": {"type": "boolean"},
            },
            "required": ["thread_id"],
        },
        get_thread,
    )

    async def switch_to_tool(args: dict) -> str:
        tool = args.get("tool", "gemini")
        max_messages = args.get("max_messages", 15)
        thread_id = args.get("thread_id")

        normalized_tool = normalize_tool_name(tool)
        if not normalized_tool or not validate_tool_name(normalized_tool):
            return "Invalid tool name. Must be one of: claude-code, cursor, gemini-cli, etc."

        if not isinstance(max_messages, int) or max_messages < 1 or max_messages > 1000:
            return "Invalid max_messages parameter (must be 1-1000)."

        if thread_id and not validate_session_id(thread_id):
            return "Invalid thread_id parameter."

        switch_tool = to_session_switch_tool(normalized_tool)
        if not switch_tool:
            return f"Tool '{normalized_tool}' is not supported by ai-session switch."

        cmd = ["ai-session", "switch", switch_tool, "--messages", str(max_messages)]
        if thread_id:
            cmd.extend(["--thread-id", thread_id])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if result.returncode == 0:
                return f"✓ Context prepared for {normalized_tool}.\n\n{result.stdout}"
            else:
                return f"Failed to switch to {normalized_tool}:\n{result.stderr}"
        except subprocess.TimeoutExpired:
            return "Timeout: Command took longer than 30 seconds."
        except FileNotFoundError:
            return "ai-session command not found."

    server.register_tool(
        "switch_to_tool",
        "Switch to another AI tool with context.",
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "max_messages": {"type": "integer"},
                "thread_id": {"type": "string"},
            },
            "required": ["tool"],
        },
        switch_to_tool,
    )
