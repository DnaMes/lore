"""Audit and build-info payload builders for the web UI (#119).

Framework-free cluster extracted from ``web.py`` so the interface module
shrinks toward its route/dispatch responsibility. Function bodies are
verbatim moves; names stay importable from ``web`` (re-exported) because
``web_jobs`` resolves them through late ``from .web import ...`` lookups
and the test suite patches them there.

Two call sites intentionally late-import from ``.web`` at call time:
``load_index`` (30+ tests patch ``web.load_index``) and the
``_current_revision`` / ``_export_fallback_scan_enabled`` environment
readers (used by web.py's security headers too).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

from lore.extractors.factory import get_all_extractors
from lore.utils.security import validate_tool_name
from lore.utils.tooling import normalize_tool_name

from .web_utils import ActionJobCancelledError


def _build_info_payload() -> dict:
    from lore import __version__
    from lore.storage.embeddings import embeddings_available, sqlite_vec_available

    from .web import _current_revision, _export_fallback_scan_enabled

    return {
        "module": __name__,
        "version": __version__,
        "revision": _current_revision(),
        "python": sys.version.split()[0],
        # Hybrid search silently degrades to FTS-only when either backend is
        # missing (#94) — surface the state so a downgrade is observable.
        "semantic": {
            "embeddings_available": embeddings_available(),
            "sqlite_vec_available": sqlite_vec_available(),
        },
        "hardening": {
            "thread_unknown_returns_404": True,
            "search_param_validation": True,
            "export_unknown_returns_404_by_default": True,
            "request_id_header": True,
            "api_rate_limiting": True,
            "health_ready_endpoints": True,
            "metrics_endpoint": True,
        },
        "export_fallback_scan_enabled": _export_fallback_scan_enabled(),
    }


def _provider_formatters() -> dict:
    return {
        "claude-code": ["strip_command_xml_tags"],
        "opencode": ["strip_local_command_caveat_in_user_messages"],
        "warp": ["drop_toolu_noise_only_assistant_chunks"],
        "default": ["normalize_newlines", "trim_whitespace"],
    }


def _new_tool_audit_row() -> dict:
    return {
        "total": 0,
        "missing_title": 0,
        "missing_thread_id": 0,
        "missing_session_id": 0,
        "empty_messages": 0,
        "missing_prompt_count": 0,
        "missing_message_count": 0,
    }


def _finalize_audit_payload(scope: str, by_tool: dict, totals: int) -> dict:
    from .web import _current_revision

    issue_count = sum(
        values["missing_title"]
        + values["missing_thread_id"]
        + values["missing_session_id"]
        + values["empty_messages"]
        + values["missing_prompt_count"]
        + values["missing_message_count"]
        for values in by_tool.values()
    )
    return {
        "scope": scope,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "revision": _current_revision(),
        "provider_formatters": _provider_formatters(),
        "totals": {
            "sessions": totals,
            "providers": len(by_tool),
            "issues": issue_count,
        },
        "uniform": issue_count == 0,
        "by_tool": by_tool,
    }


def _audit_index_sessions() -> dict:
    from .web import load_index  # late import: tests patch web.load_index

    idx = load_index()
    by_tool = {}
    sessions = idx.get("sessions", [])
    for session in sessions:
        tool = str(session.get("tool") or "unknown")
        row = by_tool.setdefault(tool, _new_tool_audit_row())
        row["total"] += 1
        if not str(session.get("id") or "").strip():
            row["missing_session_id"] += 1
        if not str(session.get("title") or "").strip():
            row["missing_title"] += 1
        if not str(session.get("thread_id") or "").strip():
            row["missing_thread_id"] += 1
        messages = int(session.get("messages") or 0)
        prompts = int(session.get("prompts") or 0)
        if messages <= 0:
            row["empty_messages"] += 1
        if prompts <= 0:
            row["missing_prompt_count"] += 1
        if messages <= 0:
            row["missing_message_count"] += 1
    return _finalize_audit_payload("index", by_tool, len(sessions))


def _audit_live_sessions(provider: Optional[str] = None, should_stop=None) -> dict:
    by_tool = {}
    total = 0
    tool_name = normalize_tool_name(provider or "") if provider else ""
    if tool_name and not validate_tool_name(tool_name):
        return _finalize_audit_payload("live", {}, 0)

    for extractor in get_all_extractors():
        if should_stop and should_stop():
            raise ActionJobCancelledError("Cancelled by user")
        if tool_name and extractor.tool.value != tool_name:
            continue
        if not extractor.is_available():
            continue
        tool = extractor.tool.value
        row = by_tool.setdefault(tool, _new_tool_audit_row())
        for session in extractor.extract_sessions():
            if should_stop and should_stop():
                raise ActionJobCancelledError("Cancelled by user")
            total += 1
            row["total"] += 1
            if not str(session.session_id or "").strip():
                row["missing_session_id"] += 1
            if not str(session.title or "").strip():
                row["missing_title"] += 1
            if not str(session.thread_id or "").strip():
                row["missing_thread_id"] += 1
            if session.message_count <= 0:
                row["empty_messages"] += 1
                row["missing_message_count"] += 1
            if session.user_prompt_count <= 0:
                row["missing_prompt_count"] += 1
    return _finalize_audit_payload("live", by_tool, total)
