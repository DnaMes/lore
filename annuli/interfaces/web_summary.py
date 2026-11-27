"""Web UI helpers for session summaries (#197 B5.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from lore.interfaces.mcp_tools.summaries import sanitize_summary_payload
from lore.interfaces.session_view_prep import at_a_glance
from lore.services.summarize import is_summary_stale
from lore.services.summarize_cli import web_summarize_disabled_reason, web_summarize_enabled
from lore.storage.summaries import get_summary, list_summaries


def summary_message_anchors(visible_pairs: Iterable[Mapping[str, Any]]) -> set[int]:
    """Message ids present on the page as ``#msg-<n>`` anchors (user prompts)."""
    anchors: set[int] = set()
    for pair in visible_pairs:
        user = pair.get("user") if isinstance(pair, Mapping) else None
        if user is None:
            continue
        index = getattr(user, "index", None)
        if isinstance(index, int):
            anchors.add(index)
    return anchors


def cite_href(seq: int, anchors: set[int]) -> Optional[str]:
    """Return ``#msg-<seq>`` when that anchor exists on the page."""
    if seq in anchors:
        return f"#msg-{seq}"
    return None


def session_summary_lines(
    output_dir,
    session_ids: Iterable[str],
    updated_by_id: Optional[Mapping[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Map session id → one-liner, model and ``outdated`` flag for list rows.

    One storage query for the whole page. ``outdated`` is a cheap timestamp comparison
    (session updated after the summary was created); the exact check runs on the detail page.
    """
    from lore.services.index import load_deleted_session_ids

    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    ids = [sid for sid in session_ids if isinstance(sid, str) and sid and sid not in deleted]
    if not ids:
        return {}
    rows = list_summaries(output_dir, level="session", session_ids=ids)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = row.get("session_id")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        one_liner = payload.get("one_liner") if isinstance(payload, dict) else None
        model = row.get("model")
        if sid and isinstance(one_liner, str) and one_liner.strip():
            out[str(sid)] = {
                "one_liner": one_liner.strip(),
                "model": str(model or ""),
                "outdated": _is_outdated(row.get("created"), (updated_by_id or {}).get(str(sid))),
            }
    return out


def _is_outdated(summary_created: Any, session_updated: Any) -> bool:
    """True when the session changed after the summary was written (cheap, no I/O)."""
    try:
        made = datetime.fromisoformat(str(summary_created).replace("Z", "+00:00"))
        changed = datetime.fromisoformat(str(session_updated).replace("Z", "+00:00"))
    except ValueError:
        return False
    if made.tzinfo is None:
        made = made.astimezone()
    if changed.tzinfo is None:
        changed = changed.astimezone()
    return changed > made


def _format_summary_created(created: Optional[str]) -> str:
    if not created:
        return ""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(created)


def build_summary_card_context(
    output_dir,
    session_id: str,
    session,
    visible_pairs: Iterable[Mapping[str, Any]],
    *,
    session_deleted: bool = False,
) -> dict[str, Any]:
    """Template context for ``session_summary_card.html``."""
    anchors = summary_message_anchors(visible_pairs)
    generate_enabled = web_summarize_enabled() and not session_deleted
    row = None if session_deleted else get_summary(output_dir, "session", f"session:{session_id}")
    if isinstance(session, dict):
        glance = {
            "first_prompt": "",
            "prompts": int(session.get("prompt_count") or 0),
            "tool_calls": 0,
            "failed_tool_calls": 0,
            "tools_used": [],
            "files_touched": [],
            "duration": None,
        }
    else:
        glance = at_a_glance(session)

    if row:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        body = sanitize_summary_payload(payload) if payload else {}
        generated = payload.get("generated_with") if isinstance(payload, dict) else None
        stale = is_summary_stale(output_dir, session_id)
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        chunks = payload.get("chunks") if isinstance(payload, dict) else None
        chunk_note = ""
        if truncated:
            chunk_note = "Summary truncated"
        elif isinstance(chunks, int) and chunks > 0:
            chunk_note = f"{chunks} chunks"
        return {
            "summary_mode": "ai",
            "summary_body": body,
            "summary_one_liner": body.get("one_liner") if body else "",
            "summary_generated_with": generated or row.get("model") or "",
            "summary_created": _format_summary_created(row.get("created")),
            "summary_stale": bool(stale),
            "summary_chunk_note": chunk_note,
            "summary_anchors": anchors,
            "summary_glance": glance,
            "summary_generate_enabled": generate_enabled,
            "summary_generate_reason": web_summarize_disabled_reason(),
            "summary_cli_command": f"lore summarize --session {session_id}",
        }

    return {
        "summary_mode": "glance",
        "summary_body": None,
        "summary_one_liner": "",
        "summary_generated_with": "",
        "summary_created": "",
        "summary_stale": False,
        "summary_chunk_note": "",
        "summary_anchors": anchors,
        "summary_glance": glance,
        "summary_generate_enabled": generate_enabled,
        "summary_generate_reason": web_summarize_disabled_reason(),
        "summary_cli_command": f"lore summarize --session {session_id}",
    }


def api_session_summary_payload(output_dir, session_id: str) -> Optional[dict[str, Any]]:
    """JSON body for ``GET /api/session/<id>/summary`` (MCP-compatible)."""
    row = get_summary(output_dir, "session", f"session:{session_id}")
    if row is None:
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    summary_body = sanitize_summary_payload(payload) if payload else None
    truncated = payload.get("truncated") if isinstance(payload, dict) else None
    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    return {
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
