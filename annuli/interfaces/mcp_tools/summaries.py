"""MCP session summary tools: ``get_session_summary``, ``summarize_session`` (#197 B4.5)."""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from typing import Any

from ...services.index import load_deleted_session_ids
from ...services.summarize import is_summary_stale
from ...services.summarize import summarize_session as run_summarize_session
from ...services.summarize_cli import (
    build_mcp_summarize_provider,
    list_indexed_session_ids,
)
from ...storage.summaries import get_summary
from ...storage.writer import v2_db_path
from ...utils.security import validate_session_id
from .deps import MCPToolDeps

_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


def _mcp_summarize_enabled() -> bool:
    return os.environ.get("LORE_MCP_ALLOW_SUMMARIZE", "").strip() == "1"


def _session_known(output_dir, session_id: str) -> bool:
    if session_id in list_indexed_session_ids(output_dir):
        return True
    if not v2_db_path(output_dir).exists():
        return False
    from ...storage.reader import load_session_messages_v2

    return bool(load_session_messages_v2(output_dir, session_id))


_SUMMARY_FIELDS = (
    "one_liner",
    "goal",
    "outcome",
    "decisions",
    "open_questions",
    "next_steps",
    "topics",
    "confidence",
)


def sanitize_summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow-list the stored payload: summary fields plus artifact paths and counts only.

    Commands and error excerpts are transcript-derived text and are never returned here.
    """
    cleaned: dict[str, Any] = {k: deepcopy(payload[k]) for k in _SUMMARY_FIELDS if k in payload}
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        out: dict[str, Any] = {}
        for key in ("files_read", "files_modified"):
            items = artifacts.get(key)
            if isinstance(items, list):
                out[key] = [i for i in items[:100] if isinstance(i, str)]
        for key in ("commands", "errors"):
            items = artifacts.get(key)
            out[f"{key}_count"] = len(items) if isinstance(items, list) else 0
        cleaned["artifacts"] = out
    return cleaned


_sanitize_summary_payload = sanitize_summary_payload


def register(server, deps: MCPToolDeps) -> None:
    """Register summary MCP tools on ``server``."""

    async def get_session_summary(args: dict) -> str:
        session_id = args.get("session_id")
        if not session_id or not validate_session_id(str(session_id)):
            return deps.json_text(
                {
                    "session_id": session_id,
                    "status": "invalid_id",
                    "stale": None,
                    "summary": None,
                    "model": None,
                    "created": None,
                    "prompt_version": None,
                    "truncated": None,
                    "chunks": None,
                }
            )

        output_dir = deps.server.output_dir
        deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
        if session_id in deleted:
            return deps.json_text(
                {
                    "session_id": session_id,
                    "status": "deleted",
                    "stale": None,
                    "summary": None,
                    "model": None,
                    "created": None,
                    "prompt_version": None,
                    "truncated": None,
                    "chunks": None,
                }
            )

        row = get_summary(output_dir, "session", f"session:{session_id}")
        if row is None:
            return deps.json_text(
                {
                    "session_id": session_id,
                    "status": "missing",
                    "stale": None,
                    "summary": None,
                    "model": None,
                    "created": None,
                    "prompt_version": None,
                    "truncated": None,
                    "chunks": None,
                }
            )

        payload = row.get("payload") or {}
        summary_body = sanitize_summary_payload(payload) if isinstance(payload, dict) else None
        truncated = payload.get("truncated") if isinstance(payload, dict) else None
        chunks = payload.get("chunks") if isinstance(payload, dict) else None
        return deps.json_text(
            {
                "session_id": session_id,
                "status": "available",
                "stale": is_summary_stale(output_dir, session_id),
                "summary": summary_body,
                "model": row.get("model"),
                "created": row.get("created"),
                "prompt_version": row.get("prompt_version"),
                "truncated": truncated,
                "chunks": chunks,
            }
        )

    server.register_tool(
        "get_session_summary",
        "Read the stored AI summary for a session (JSON data only; no transcript text).",
        {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        get_session_summary,
        annotations={"readOnlyHint": True},
    )

    async def summarize_session_tool(args: dict) -> str:
        if not _mcp_summarize_enabled():
            return deps.json_text(
                {
                    "error": "summarize_session is disabled",
                    "hint": "set LORE_MCP_ALLOW_SUMMARIZE=1",
                }
            )

        session_id = args.get("session_id")
        if not session_id or not validate_session_id(str(session_id)):
            return deps.json_text({"error": "Invalid session_id parameter."})

        output_dir = deps.server.output_dir
        deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
        if session_id in deleted:
            return deps.json_text({"error": "Session is deleted", "session_id": session_id})
        if not _session_known(output_dir, session_id):
            return deps.json_text({"error": "Session not found", "session_id": session_id})

        force = bool(args.get("force", False))
        with _session_lock(str(session_id)):
            # Re-check under the lock: the session may have been deleted while this call waited.
            if session_id in load_deleted_session_ids(output_dir / "deleted_sessions.json"):
                return deps.json_text({"error": "Session is deleted", "session_id": session_id})
            try:
                provider, model_label = build_mcp_summarize_provider()
            except ValueError as exc:
                return deps.json_text({"error": str(exc)})

            if not provider.is_available():
                return deps.json_text(
                    {
                        "error": "Local summarize provider is not available",
                        "hint": "Start Ollama locally",
                    }
                )

            outcome = run_summarize_session(
                provider,
                output_dir,
                str(session_id),
                model_label=model_label,
                force=force,
            )
            summary_available = outcome in ("written", "unchanged") and (
                get_summary(output_dir, "session", f"session:{session_id}") is not None
            )
            return deps.json_text(
                {
                    "session_id": session_id,
                    "outcome": outcome,
                    "summary_available": summary_available,
                }
            )

    server.register_tool(
        "summarize_session",
        "Generate or refresh the AI summary for one session (local Ollama only; "
        "disabled unless LORE_MCP_ALLOW_SUMMARIZE=1). Does not return summary text.",
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["session_id"],
        },
        summarize_session_tool,
        annotations={"readOnlyHint": False},
    )
