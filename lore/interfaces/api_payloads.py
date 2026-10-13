"""Shared payload builders for MCP and JSON API responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..storage.fts import snippet_to_text


def _isoformat(value: Any) -> Optional[str]:
    """Return a stable ISO-like string for datetime-ish values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def serialize_index_session_summary(session_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize indexed session metadata for list/search responses."""
    return {
        "id": session_meta.get("id"),
        "tool": session_meta.get("tool"),
        "title": session_meta.get("title"),
        "display_title": session_meta.get("display_title") or session_meta.get("title"),
        "created": session_meta.get("created"),
        "updated": session_meta.get("updated"),
        "project": session_meta.get("project"),
        "thread_id": session_meta.get("thread_id"),
        "message_count": int(session_meta.get("messages") or 0),
        "prompt_count": int(session_meta.get("prompts") or 0),
        "export_path": session_meta.get("export_path"),
        "origin": session_meta.get("origin") or "unknown",
        "parent_session_id": session_meta.get("parent_session_id"),
        "relation": session_meta.get("relation"),
        "source_version": session_meta.get("source_version"),
        "parser_version": int(session_meta.get("parser_version") or 0),
    }


def search_result_snippet(result: Dict[str, Any]) -> Optional[str]:
    """Return a search result's snippet as text with ``«``/``»`` marks, or None."""
    snippet = result.get("snippet")
    return snippet_to_text(snippet) if snippet else None


def serialize_search_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize one ``search_index`` hit: session summary, score, snippet, source."""
    return {
        **serialize_index_session_summary(result["session"]),
        "score": result.get("score"),
        "snippet": search_result_snippet(result),
        "source": "session",
    }


def serialize_live_message(message: Any, index: Optional[int] = None) -> Dict[str, Any]:
    """Serialize a live UnifiedMessage for API/MCP responses."""
    return {
        "index": index,
        "message_id": getattr(message, "message_id", None),
        "role": getattr(getattr(message, "role", None), "value", None),
        "timestamp": _isoformat(getattr(message, "timestamp", None)),
        "content": getattr(message, "content", None),
        "model": getattr(message, "model", None),
        "tokens": getattr(message, "tokens", None),
        "tool_calls": getattr(message, "tool_calls", []) or [],
        "reasoning": getattr(message, "reasoning", None),
    }


def serialize_live_session(
    session: Any,
    session_meta: Optional[Dict[str, Any]] = None,
    include_messages: bool = False,
) -> Dict[str, Any]:
    """Serialize a live UnifiedSession for detail responses."""
    payload = {
        "id": getattr(session, "session_id", None),
        "tool": getattr(getattr(session, "tool", None), "value", None),
        "title": getattr(session, "title", None) or (session_meta or {}).get("title"),
        "created": _isoformat(getattr(session, "created_at", None))
        or (session_meta or {}).get("created"),
        "updated": _isoformat(getattr(session, "last_updated", None))
        or (session_meta or {}).get("updated"),
        "project": getattr(session, "project_path", None) or (session_meta or {}).get("project"),
        "thread_id": getattr(session, "thread_id", None) or (session_meta or {}).get("thread_id"),
        "message_count": getattr(session, "message_count", 0),
        "prompt_count": getattr(session, "user_prompt_count", 0),
        "assistant_message_count": getattr(session, "assistant_message_count", 0),
        "total_tokens": getattr(session, "total_tokens", None),
        "summary": getattr(session, "summary", None),
        "git_branch": getattr(session, "git_branch", None),
        "git_commit": getattr(session, "git_commit", None),
        "source_path": getattr(session, "source_path", None),
        "cli_version": getattr(session, "cli_version", None),
        "export_path": (session_meta or {}).get("export_path"),
        "origin": getattr(session, "origin", None) or "unknown",
        "parent_session_id": getattr(session, "parent_session_id", None),
        "relation": getattr(session, "relation", None),
        "source_version": getattr(session, "source_version", None),
        "parser_version": int(getattr(session, "parser_version", 0) or 0),
        "live": True,
    }
    if include_messages:
        payload["messages"] = [
            serialize_live_message(message, index=index)
            for index, message in enumerate(getattr(session, "messages", []))
        ]
    return payload


def serialize_thread_overview(thread: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a thread overview row."""
    return {
        "id": thread.get("thread_id"),
        "title": thread.get("title"),
        "project": thread.get("project"),
        "session_count": int(thread.get("count") or 0),
        "updated": thread.get("updated"),
    }


def serialize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a project payload for external clients."""
    return {
        "name": project.get("name"),
        "path": project.get("path"),
        "session_count": int(project.get("session_count") or 0),
        "prompt_count": int(project.get("prompt_count") or 0),
        "tools": list(project.get("tools") or []),
        "tool_counts": dict(project.get("tool_counts") or {}),
        "updated": project.get("updated"),
        "recent_sessions": [
            serialize_index_session_summary(session)
            for session in (project.get("recent_sessions") or [])
        ],
    }


def serialize_thread_messages(
    messages: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Serialize thread timeline messages already prepared by web services."""
    payload = []
    for index, message in enumerate(messages):
        payload.append(
            {
                "index": index,
                "role": getattr(message.get("role"), "value", None),
                "timestamp": _isoformat(message.get("timestamp")),
                "content_html": message.get("content"),
                "raw_content": message.get("raw_content"),
                "label": message.get("label"),
                "style": message.get("style"),
            }
        )
    return payload
