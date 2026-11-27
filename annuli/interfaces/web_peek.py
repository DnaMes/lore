"""Server-rendered session peek pane fragment (B5.7b)."""

from __future__ import annotations

from typing import Any, Mapping

from lore.interfaces.mcp_tools.summaries import sanitize_summary_payload
from lore.interfaces.session_view_prep import at_a_glance
from lore.interfaces.web_formatting import format_session_duration, format_session_row_time
from lore.interfaces.web_helpers import get_style, project_label
from lore.storage.summaries import get_summary


def build_peek_context(
    output_dir,
    session_id: str,
    session_meta: Mapping[str, Any],
    session_obj: Any,
) -> dict[str, Any]:
    """Template context for ``session_peek.html``."""
    title = session_meta.get("display_title") or session_meta.get("title") or session_id
    tool = str(session_meta.get("tool") or "")
    style = get_style(tool)
    row_time = format_session_row_time(session_meta.get("updated") or session_meta.get("created"))
    duration = format_session_duration(
        session_meta.get("created"),
        session_meta.get("updated"),
    )
    meta_bits = [str(style.get("name") or tool), project_label(session_meta.get("project"))]
    if row_time[0]:
        meta_bits.append(row_time[0])
    if duration:
        meta_bits.append(duration)

    glance = (
        at_a_glance(session_obj) if session_obj is not None else _glance_from_index(session_meta)
    )
    summary_mode = "glance"
    summary_one_liner = ""
    row = get_summary(output_dir, "session", f"session:{session_id}")
    if row:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        body = sanitize_summary_payload(payload) if payload else {}
        one_liner = body.get("one_liner") if isinstance(body, dict) else None
        if isinstance(one_liner, str) and one_liner.strip():
            summary_mode = "ai"
            summary_one_liner = one_liner.strip()

    return {
        "session_id": session_id,
        "title": title,
        "tool_style": style,
        "meta_line": " · ".join(meta_bits),
        "summary_mode": summary_mode,
        "summary_one_liner": summary_one_liner,
        "glance": glance,
    }


def _clamp_peek_prompt(text: str, limit: int = 240) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _glance_from_index(session_meta: Mapping[str, Any]) -> dict[str, Any]:
    outline = str(session_meta.get("prompt_outline") or "")
    first = _clamp_peek_prompt(outline) if outline else ""
    return {
        "first_prompt": first,
        "prompts": int(session_meta.get("prompts") or session_meta.get("messages") or 0),
        "tool_calls": 0,
        "failed_tool_calls": 0,
        "tools_used": [],
        "files_touched": [],
        "duration": format_session_duration(
            session_meta.get("created"),
            session_meta.get("updated"),
        ),
    }
