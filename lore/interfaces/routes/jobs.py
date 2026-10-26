"""Reload/audit job endpoints and the incremental index reload (#119)."""

import json
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

from lore.interfaces import web as _web
from lore.utils.security import validate_tool_name
from lore.utils.tooling import normalize_tool_name

from ..web_data import _build_index_from_extractors, clear_index_cache, clear_sessions_cache
from ..web_jobs import (
    _cancel_action_job,
    _get_reload_job,
    _run_audit,
)
from ..web_utils import ActionJobCancelledError

bp = Blueprint("jobs", __name__)


def _reload_sessions_index(
    provider: Optional[str] = None,
    progress_callback=None,
    should_stop=None,
    incremental: bool = True,
) -> dict:
    start = datetime.now(timezone.utc)

    tool_filter = normalize_tool_name(provider or "") or ""
    if tool_filter and not validate_tool_name(tool_filter):
        return {"status": "error", "error": "Invalid provider"}

    if progress_callback:
        progress_callback(5, "Loading current index")
    if should_stop and should_stop():
        raise ActionJobCancelledError("Cancelled by user")
    existing_idx = _web.load_index()
    existing_sessions = existing_idx.get("sessions", [])

    if progress_callback:
        progress_callback(15, "Collecting provider sessions")
    extractor_errors = (
        _build_index_from_extractors(
            tool_filter=tool_filter or None,
            progress_callback=progress_callback,
            should_stop=should_stop,
            incremental=incremental,
        )
        or []
    )

    if progress_callback:
        progress_callback(65, "Loading refreshed index")
    if should_stop and should_stop():
        raise ActionJobCancelledError("Cancelled by user")
    refreshed_idx = _web.load_index()
    refreshed_sessions = refreshed_idx.get("sessions", [])
    refreshed_tools = {str(session.get("tool") or "") for session in refreshed_sessions}

    merged_by_id = {}
    if tool_filter:
        for session in existing_sessions:
            if str(session.get("tool") or "") != tool_filter:
                sid = str(session.get("id") or "")
                if sid:
                    merged_by_id[sid] = session
    else:
        for session in existing_sessions:
            tool = str(session.get("tool") or "")
            if tool not in refreshed_tools:
                sid = str(session.get("id") or "")
                if sid:
                    merged_by_id[sid] = session

    for session in refreshed_sessions:
        sid = str(session.get("id") or "")
        if sid:
            merged_by_id[sid] = session

    if progress_callback:
        progress_callback(80, "Rebuilding stats")
    if should_stop and should_stop():
        raise ActionJobCancelledError("Cancelled by user")
    merged_sessions = sorted(
        merged_by_id.values(), key=lambda row: row.get("created", ""), reverse=True
    )

    by_tool = {}
    by_project = {}
    total_messages = 0
    for session in merged_sessions:
        tool = str(session.get("tool") or "")
        if tool:
            by_tool[tool] = by_tool.get(tool, 0) + 1
        project = session.get("project")
        if project:
            by_project[project] = by_project.get(project, 0) + 1
        total_messages += int(session.get("messages") or 0)

    merged_idx = {
        "version": refreshed_idx.get("version", existing_idx.get("version", "1.0.0")),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stats": {
            "total_sessions": len(merged_sessions),
            "total_messages": total_messages,
            "by_tool": by_tool,
            "by_project": by_project,
        },
        "sessions": merged_sessions,
    }

    with open(_web.INDEX_PATH, "w", encoding="utf-8") as handle:
        json.dump(merged_idx, handle, indent=2)

    clear_sessions_cache()
    clear_index_cache()
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    if progress_callback:
        progress_callback(100, "Completed")
    return {
        "status": "ok",
        "provider": tool_filter or "all",
        "reload_seconds": round(elapsed, 3),
        "total_sessions": len(merged_sessions),
        "by_tool": by_tool,
        "refreshed_tools": sorted([tool for tool in refreshed_tools if tool]),
        "revision": _web._current_revision(),
        "mode": "incremental" if incremental else "full",
        "errors": extractor_errors,
    }


@bp.route("/api/audit")
def api_audit():
    scope = (request.args.get("scope") or "index").strip().lower()
    provider = normalize_tool_name((request.args.get("provider") or "").strip()) or ""
    if provider and not validate_tool_name(provider):
        return jsonify({"error": "Invalid provider"}), 400

    async_mode = (request.args.get("async") or "0").strip() == "1"
    if async_mode:
        job_id = _web._start_audit_job(scope, provider or None)
        return (
            jsonify(
                {
                    "status": "accepted",
                    "job_id": job_id,
                    "provider": provider or "all",
                    "scope": scope,
                }
            ),
            202,
        )

    return jsonify(_run_audit(scope, provider or None))


@bp.route("/api/reload-sessions", methods=["POST"])
def api_reload_sessions():
    if (err := _web._check_local_origin()) is not None:
        return err
    provider = normalize_tool_name((request.args.get("provider") or "").strip()) or ""
    if provider and not validate_tool_name(provider):
        return jsonify({"status": "error", "error": "Invalid provider"}), 400

    full_rebuild = (request.args.get("full") or "0").strip() == "1"
    incremental = not full_rebuild

    async_mode = (request.args.get("async") or "0").strip() == "1"
    if async_mode:
        job_id = _web._start_reload_job(provider or None, incremental=incremental)
        return (
            jsonify(
                {
                    "status": "accepted",
                    "job_id": job_id,
                    "provider": provider or "all",
                    "mode": "incremental" if incremental else "full",
                }
            ),
            202,
        )

    return jsonify(_web._reload_sessions_index(provider=provider or None, incremental=incremental))


@bp.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    """Clear all in-memory caches to force reload from disk."""
    if (err := _web._check_local_origin()) is not None:
        return err
    clear_index_cache()
    clear_sessions_cache()
    return jsonify({"status": "ok", "message": "Cache cleared"})


@bp.route("/api/reload-status/<job_id>")
def api_reload_status(job_id):
    state = _get_reload_job(job_id)
    if not state:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(state)


@bp.route("/api/audit-status/<job_id>")
def api_audit_status(job_id):
    state = _get_reload_job(job_id)
    if not state or state.get("kind") != "audit":
        return jsonify({"error": "Job not found"}), 404
    return jsonify(state)


@bp.route("/api/action-cancel/<job_id>", methods=["POST"])
def api_action_cancel(job_id):
    if (err := _web._check_local_origin()) is not None:
        return err
    state = _get_reload_job(job_id)
    if not state:
        return jsonify({"error": "Job not found"}), 404
    if str(state.get("status") or "") in {"done", "error", "cancelled"}:
        return jsonify(
            {
                "status": "noop",
                "job_id": job_id,
                "message": "Job already finished",
            }
        )

    _cancel_action_job(job_id)
    return jsonify(
        {
            "status": "accepted",
            "job_id": job_id,
            "message": "Cancellation requested",
        }
    )
