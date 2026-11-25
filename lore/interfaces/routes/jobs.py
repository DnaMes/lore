"""Reload/audit job endpoints and the incremental index reload (#119)."""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

from lore.interfaces import web as _web
from lore.utils.security import validate_tool_name
from lore.utils.tooling import normalize_tool_name

from ..web_data import _build_index_from_extractors, clear_index_cache, clear_sessions_cache
from ..web_jobs import (
    APP_STARTED_EPOCH,
    _cancel_action_job,
    _get_reload_job,
    _run_audit,
)
from ..web_utils import ActionJobCancelledError, _metrics_snapshot

bp = Blueprint("jobs", __name__)


@bp.route("/api/build-info")
def api_build_info():
    return jsonify(_web._build_info_payload())


@bp.route("/api/status")
def api_status():
    """Return lightweight operational status for the web UI status bar."""
    from lore import __version__
    from lore.services import index as _index_service
    from lore.services import summarize_cli as _summarize_cli
    from lore.services import worker as _worker_service
    from lore.storage import v2_db_path
    from lore.storage.freshness import store_freshness
    from lore.storage.message_fts import message_fts_pending, message_fts_ready
    from lore.storage.schema import open_connection

    errors: list[str] = []

    sessions_info: Optional[dict[str, Any]] = None
    try:
        summary = _web.load_index_summary()
        sessions_info = {
            "total_sessions": int(summary.get("total_sessions") or 0),
            "automated_hidden": int(summary.get("automated_hidden") or 0),
            "last_updated": summary.get("last_updated"),
        }
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"sessions: {exc}")

    index_info: Optional[dict[str, Any]] = None
    try:
        db_path = v2_db_path(_web.INDEX_PATH.parent)
        freshness = store_freshness(db_path, compare_to=_web.INDEX_PATH)
        index_info = {
            "fresh": bool(freshness.get("ok")),
            "reasons": list(freshness.get("reasons") or []),
        }
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"index: {exc}")

    search_info: Optional[dict[str, Any]] = None
    try:
        db_path = v2_db_path(_web.INDEX_PATH.parent)
        build_info = _web._build_info_payload() or {}
        semantic = build_info.get("semantic") or {}
        is_hybrid = bool(
            _index_service._hybrid_enabled()
            and semantic.get("embeddings_available")
            and semantic.get("sqlite_vec_available")
        )
        mode = "hybrid" if is_hybrid else "keyword"

        if not db_path.exists():
            message_index = "absent"
        else:
            conn = open_connection(db_path)
            try:
                if message_fts_ready(conn):
                    message_index = "ready"
                elif message_fts_pending(conn):
                    message_index = "backfilling"
                else:
                    message_index = "absent"
            finally:
                conn.close()

        search_info = {
            "mode": mode,
            "message_index": message_index,
        }
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"search: {exc}")

    worker_info: Optional[dict[str, Any]] = None
    try:
        worker_status = _worker_service.load_status(_web.OUTPUT_DIR)
        mtime = worker_status.get("heartbeat_mtime")
        interval = int(worker_status.get("interval_seconds") or 0)
        if mtime is None:
            state_exists = (_worker_service.worker_dir(_web.OUTPUT_DIR) / "state.json").is_file()
            worker_info = {
                "state": "stale" if state_exists else "absent",
                "heartbeat_age_seconds": None,
            }
        else:
            age = max(0, int(time.time() - float(mtime)))
            worker_info = {
                "state": "stale" if age > interval * 3 else "ok",
                "heartbeat_age_seconds": age,
            }
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"worker: {exc}")

    llm_info: Optional[dict[str, Any]] = None
    try:
        raw_provider = os.environ.get("LORE_SUMMARY_PROVIDER", "").strip()
        if raw_provider:
            provider: Optional[str] = raw_provider.lower()
            local: Optional[bool] = _summarize_cli.provider_locality(provider) == "local"
        else:
            provider = None
            local = None
        llm_info = {
            "provider": provider,
            "local": local,
            "web_summaries": bool(_summarize_cli.web_summarize_enabled()),
        }
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"llm: {exc}")

    version_val: Optional[str] = None
    revision_val: Optional[str] = None
    try:
        version_val = __version__
        revision_val = _web._current_revision()
    except Exception as exc:  # noqa: BLE001 — status endpoint must never raise
        errors.append(f"version: {exc}")

    return jsonify(
        {
            "sessions": sessions_info,
            "index": index_info,
            "search": search_info,
            "worker": worker_info,
            "llm": llm_info,
            "version": version_val,
            "revision": revision_val,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "errors": errors,
        }
    )


@bp.route("/api/health")
def api_health():
    uptime_seconds = max(0, int(time.time() - APP_STARTED_EPOCH))
    return jsonify(
        {
            "status": "ok",
            "revision": _web._current_revision(),
            "uptime_seconds": uptime_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )


@bp.route("/api/ready")
def api_ready():
    checks: dict[str, Any] = {
        "output_dir_writable": False,
        "index_path_parent_exists": False,
    }
    errors: list[str] = []

    try:
        _web.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        checks["output_dir_writable"] = _web.OUTPUT_DIR.exists() and os.access(
            _web.OUTPUT_DIR, os.W_OK
        )
    except OSError as exc:
        errors.append(f"output_dir_error: {exc}")

    checks["index_path_parent_exists"] = _web.INDEX_PATH.parent.exists()
    checks["index_exists"] = _web.INDEX_PATH.exists()

    ready = bool(checks["output_dir_writable"] and checks["index_path_parent_exists"])
    payload = {
        "ready": ready,
        "status": "ready" if ready else "degraded",
        "revision": _web._current_revision(),
        "checks": checks,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return jsonify(payload), (200 if ready else 503)


@bp.route("/api/metrics")
def api_metrics():
    metrics_snapshot = _metrics_snapshot()
    uptime_seconds = max(0, int(time.time() - APP_STARTED_EPOCH))
    out_format = (request.args.get("format") or "json").strip().lower()

    if out_format in {"prom", "prometheus", "text"}:
        lines = [
            "# HELP lore_uptime_seconds Process uptime in seconds",
            "# TYPE lore_uptime_seconds gauge",
            f"lore_uptime_seconds {uptime_seconds}",
        ]
        for key in sorted(metrics_snapshot.keys()):
            metric_name = f"lore_{key}"
            lines.append(f"# TYPE {metric_name} counter")
            lines.append(f"{metric_name} {metrics_snapshot[key]}")
        body = "\n".join(lines) + "\n"
        return Response(body, mimetype="text/plain")

    payload = {
        "revision": _web._current_revision(),
        "uptime_seconds": uptime_seconds,
        "metrics": metrics_snapshot,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return jsonify(payload)


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
