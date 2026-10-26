"""MCP session tools: get_session, get_session_messages, list_recent_sessions,
list_projects, get_thread and switch_to_tool."""

from __future__ import annotations

import json
import shutil
import subprocess

from ...services.index import (
    InvalidFlagError,
    automated_hidden_line,
    filter_automated,
    parse_bool_flag,
    thread_hidden_count,
    thread_sessions,
)
from ...services.summarize_cli import cloud_opt_in_granted
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

SESSION_COMMAND = "lore-session"

# Tools whose lore-session switch launches a process that receives OUR session
# context (gemini and codex, both via a @file reference). They need an explicit cloud opt-in.
CLOUD_CONTEXT_TOOLS = frozenset({"gemini", "codex"})


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
        try:
            include_automated = parse_bool_flag(
                args.get("include_automated"), name="include_automated"
            )
        except InvalidFlagError as exc:
            return str(exc)
        sessions = sorted(
            filter_automated(deps.ensure_index().get("sessions", []), include_automated),
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
                "include_automated": {
                    "type": "boolean",
                    "description": "Also list exec/cron/subagent sessions (hidden by default).",
                },
            },
        },
        list_recent_sessions,
    )

    async def list_projects(args: dict) -> str:
        limit = deps.normalize_limit(args.get("limit"), default=50)
        try:
            include_automated = parse_bool_flag(
                args.get("include_automated"), name="include_automated"
            )
        except InvalidFlagError as exc:
            return str(exc)
        projects = build_projects_payload(
            filter_automated(deps.ensure_index().get("sessions", []), include_automated),
            lambda value: value,
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
                "include_automated": {
                    "type": "boolean",
                    "description": "Also count exec/cron/subagent sessions (hidden by default).",
                },
            },
        },
        list_projects,
    )

    async def get_thread(args: dict) -> str:
        thread_id = args.get("thread_id")
        include_messages = bool(args.get("include_messages", False))
        try:
            include_automated = parse_bool_flag(
                args.get("include_automated"), name="include_automated"
            )
        except InvalidFlagError as exc:
            return str(exc)

        if not thread_id or not validate_session_id(thread_id):
            return "Invalid thread_id parameter."

        idx = deps.ensure_index()
        # A thread of automated sessions only stays reachable by its id.
        index_sessions = thread_sessions(idx.get("sessions", []), thread_id, include_automated)
        # O(1) bucket hit via the memoized {thread_id: overview} map (#126)
        # instead of regrouping every indexed session per call.
        overview = thread_overview_by_id(index_sessions).get(thread_id)
        if not overview:
            return deps.json_text({"error": "Thread not found", "thread_id": thread_id})

        payload: dict[str, object] = {"thread": serialize_thread_overview(overview)}
        hidden = thread_hidden_count(idx.get("sessions", []), thread_id, include_automated)
        if hidden:
            payload["automated_hidden"] = hidden
            payload["automated_hidden_line"] = automated_hidden_line(hidden)
        if include_messages:
            detail = build_thread_detail_payload(
                thread_id,
                index_sessions,
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
                "include_automated": {
                    "type": "boolean",
                    "description": "Include exec/cron/subagent sessions of the thread "
                    "(hidden by default).",
                },
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
            return f"Tool '{normalized_tool}' is not supported by lore-session switch."

        if shutil.which(SESSION_COMMAND) is None:
            return (
                f"{SESSION_COMMAND} command not found. "
                "Install it with: pipx install git+https://github.com/DnaMes/lore"
            )

        # Cloud gate: refuse before spawning anything when the requested tool
        # would receive the session context in a cloud-bound process.
        if switch_tool in CLOUD_CONTEXT_TOOLS and not cloud_opt_in_granted():
            return (
                f"Refusing to switch to {normalized_tool}: lore-session would send the "
                "session context to the cloud. Set LORE_ALLOW_CLOUD=1 to confirm."
            )

        cmd = [SESSION_COMMAND, "switch", switch_tool, "--messages", str(max_messages)]
        if switch_tool in CLOUD_CONTEXT_TOOLS:
            cmd.append("--allow-cloud")
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
            return (
                f"{SESSION_COMMAND} command not found. "
                "Install it with: pipx install git+https://github.com/DnaMes/lore"
            )

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
