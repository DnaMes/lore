import html
import importlib
import json
import logging
import math
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import markdown
import nh3
from flask import Flask, Response, g, has_request_context, jsonify, redirect, request
from werkzeug.middleware.proxy_fix import ProxyFix

from lore.extractors.factory import get_all_extractors
from lore.extractors.opencode import OpenCodeExtractor
from lore.utils.security import (
    validate_search_param,
    validate_session_id,
    validate_tool_name,
)
from lore.utils.text_processing import format_thinking
from lore.utils.tooling import normalize_tool_name

from ..services.cache import threadsafe_lru_cache
from ..services.extraction import find_live_session
from ..services.index import session_by_id_map
from .api_payloads import (
    serialize_index_session_summary,
    serialize_live_session,
    serialize_project,
    serialize_thread_messages,
    serialize_thread_overview,
)
from .web_data import (
    DELETED_SESSIONS_PATH,
    INDEX_PATH,
    OUTPUT_DIR,
    _build_index_from_extractors,
    _sanitize_next_url,
    build_session_from_export_markdown,
    clear_index_cache,
    clear_sessions_cache,
    load_export_lookup,
    load_index,
    load_index_summary,
    load_sessions_for_tool,
    remember_deleted_session_id,
    resolve_export_path,
    search_index,
)
from .web_formatting import (
    SANITIZE_ATTRS,
    SANITIZE_TAGS,
    SANITIZE_URL_SCHEMES,
    format_message_content,
    format_tool_calls,
    sanitize_rendered_html,
)
from .web_helpers import (
    TOOL_STYLES,
    filter_sessions,
    get_style,
    parse_date_param,
    project_label,
)
from .web_jobs import (
    ACTION_JOB_TIMEOUT_SECONDS,
    APP_STARTED_EPOCH,
    RELOAD_JOB_MAX,
    RELOAD_JOB_TTL_SECONDS,
    RELOAD_JOBS,
    RELOAD_JOBS_LOCK,
    _assert_job_active,
    _cancel_action_job,
    _get_reload_job,
    _prune_reload_jobs_locked,
    _run_audit,
    _set_reload_job,
    _start_audit_job,
    _start_reload_job,
)
from .web_utils import (
    METRICS,
    METRICS_LOCK,
    NOISE_RULES_PATH,
    RATE_LIMIT_STATE,
    ActionJobCancelledError,
    ActionJobTimeoutError,
    _client_ip,
    _consume_rate_limit,
    _json_request_logging_enabled,
    _metrics_inc,
    _metrics_snapshot,
    _noise_rule_presets,
    _rate_limit_enabled,
    _record_job_outcome,
    _request_id,
    _should_rate_limit,
    load_noise_rules,
    save_noise_rules,
)

_TEST_EXPORTS = (
    DELETED_SESSIONS_PATH,
    ACTION_JOB_TIMEOUT_SECONDS,
    RELOAD_JOB_MAX,
    RELOAD_JOB_TTL_SECONDS,
    RELOAD_JOBS,
    RELOAD_JOBS_LOCK,
    _assert_job_active,
    _prune_reload_jobs_locked,
    _set_reload_job,
    ActionJobTimeoutError,
    METRICS,
    METRICS_LOCK,
    NOISE_RULES_PATH,
    RATE_LIMIT_STATE,
    _record_job_outcome,
)

_web_services = importlib.import_module("lore.interfaces.web_services")
build_projects_payload = _web_services.build_projects_payload
build_thread_detail_payload = _web_services.build_thread_detail_payload
build_threads_overview = _web_services.build_threads_overview
compute_top_tags = _web_services.compute_top_tags
enrich_session_for_detail = _web_services.enrich_session_for_detail
preview_normalize_message = _web_services.preview_normalize_message

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=Path(__file__).parent.parent / "templates",
    # Vendored Tailwind / highlight.js live in interfaces/static/ so the UI
    # works fully offline (air-gapped installs) — no CDN dependency.
    static_folder=Path(__file__).parent / "static",
    static_url_path="/static",
)

_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if not _flask_secret:
    _flask_secret = secrets.token_hex(32)
    logger.warning(
        "FLASK_SECRET_KEY not set — using ephemeral key. "
        "Sessions will break on restart. "
        "Set FLASK_SECRET_KEY env var for production."
    )
app.config["SECRET_KEY"] = _flask_secret
# SameSite=Strict stops cross-site requests from carrying the session cookie.
# Combined with the _check_local_origin() guard on destructive routes this
# replaces Flask-WTF CSRF tokens (which don't survive ephemeral SECRET_KEY restarts).
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# Only trust X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host when running
# behind a known reverse proxy. Without this guard any client can spoof their IP
# (used by the rate-limiter) or escalate http→https by injecting headers directly.
# Set TRUSTED_PROXY=1 (or any non-empty string) when a proxy sits in front.
if os.environ.get("TRUSTED_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]


_SESSION_WATCHER = None


def _maybe_start_session_watcher() -> None:
    """Start the background file watcher when ``LORE_WATCH=1``.

    The watcher polls every extractor's data dir and clears the index cache
    whenever any of them change. The next request rebuilds the index lazily.
    """
    global _SESSION_WATCHER
    if _SESSION_WATCHER is not None:
        return
    if os.environ.get("LORE_WATCH", "").strip() not in ("1", "true", "True"):
        return
    try:
        interval = float(os.environ.get("LORE_WATCH_INTERVAL", "30"))
    except ValueError:
        interval = 30.0
    try:
        from lore.watcher import SessionWatcher

        watcher = SessionWatcher(callback=clear_index_cache, interval=interval)
        watcher.start()
        _SESSION_WATCHER = watcher
        logger.info("Session watcher enabled (interval=%.1fs)", interval)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start session watcher")


_maybe_start_session_watcher()


def _check_local_origin() -> Optional[tuple]:
    """Reject cross-origin POST requests to destructive endpoints.

    Browsers always send Origin on cross-origin requests. curl/MCP clients
    never send Origin, so they are unaffected. Returns an error Response if
    the request should be rejected, None otherwise.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return None
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return None
    return jsonify({"error": "Cross-origin request rejected"}), 403


def _current_revision() -> str:
    return os.environ.get("LORE_BUILD_SHA") or os.environ.get("LORE_BUILD_REVISION") or "dev"


def _export_fallback_scan_enabled() -> bool:
    return os.environ.get("LORE_EXPORT_FALLBACK_SCAN", "").lower() == "true"


def _build_info_payload() -> dict:
    from lore import __version__
    from lore.storage.embeddings import embeddings_available, sqlite_vec_available

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
    existing_idx = load_index()
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
    refreshed_idx = load_index()
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

    with open(INDEX_PATH, "w", encoding="utf-8") as handle:
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
        "revision": _current_revision(),
        "mode": "incremental" if incremental else "full",
        "errors": extractor_errors,
    }


# Clear OpenCode cache if requested
if os.environ.get("LORE_CLEAR_OPENCODE_CACHE", "").lower() == "true":
    opencode_state_cache = OUTPUT_DIR / "cache" / "opencode_state.json"
    if opencode_state_cache.exists():
        opencode_state_cache.unlink()


# Templates live as files under lore/templates/*.html, loaded by _template_env().

# --- LOGIC ---


TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


@lru_cache(maxsize=1)
def _template_env():
    """Build the Jinja Environment once (templates are static files in the wheel).

    Replaces the previous per-request Environment(FunctionLoader(dict)) — Jinja now
    compiles+caches each template on first use. autoescape=True for all loaded
    templates matches the prior default_for_string=True string-loader behavior (#30).
    """
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env.filters["urlpath"] = lambda value: quote(str(value or ""), safe="")
    return env


def render(tpl_name, **kwargs):
    env = _template_env()
    idx = load_index()
    recent = sorted(
        idx.get("sessions", []),
        key=lambda s: s.get("updated") or s.get("created") or "",
        reverse=True,
    )[:8]
    if "nav_back" not in kwargs:
        if has_request_context():
            nav_back = (
                request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
            )
            kwargs["nav_back"] = nav_back or "/"
        else:
            kwargs["nav_back"] = "/"
    if "show_session_controls" not in kwargs:
        if has_request_context():
            kwargs["show_session_controls"] = request.path.startswith(
                "/session/"
            ) or request.path.startswith("/thread/")
        else:
            kwargs["show_session_controls"] = tpl_name in ("session", "thread_detail")
    from lore import __version__

    return env.get_template(f"{tpl_name}.html").render(
        get_style=get_style,
        project_label=project_label,
        recent=recent,
        provider_tools=list(TOOL_STYLES.keys()),
        request=request if has_request_context() else None,
        nonce=get_csp_nonce(),
        app_version=__version__,
        **kwargs,
    )


def get_csp_nonce() -> str:
    """Return the per-request CSP nonce, with a safe fallback for test contexts.

    Falls back to an empty string when called outside a request context or when
    ``before_request`` has not been invoked (e.g. direct route calls in tests).
    """
    try:
        return g.csp_nonce  # type: ignore[no-any-return]
    except (RuntimeError, AttributeError):
        # RuntimeError: no active request context.
        # AttributeError: request context exists but before_request was skipped.
        return ""


@app.before_request
def prepare_request_context_and_limit() -> Optional[Response]:
    g.csp_nonce = secrets.token_urlsafe(16)
    g.request_started_epoch = time.time()
    g.request_id = _request_id()

    if not _rate_limit_enabled() or not _should_rate_limit(request.path):
        return None

    client_ip = _client_ip()
    allowed, retry_after, remaining, limit, reset_in = _consume_rate_limit(request.path, client_ip)
    g.rate_limit_limit = limit
    g.rate_limit_remaining = remaining
    g.rate_limit_reset_in = reset_in

    if allowed:
        return None

    _metrics_inc("rate_limit_rejections")
    logger.warning(
        "Rate limit exceeded for ip=%s path=%s retry_after=%ss request_id=%s",
        client_ip,
        request.path,
        retry_after,
        g.request_id,
    )
    response = jsonify(
        {
            "error": "Rate limit exceeded",
            "retry_after_seconds": retry_after,
            "request_id": g.request_id,
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = "0"
    response.headers["X-RateLimit-Reset"] = str(reset_in)
    return response


@app.after_request
def add_security_headers(response):
    nonce = get_csp_nonce()
    nonce_src = f"'nonce-{nonce}'" if nonce else ""
    # All assets (Tailwind, highlight.js) are vendored under /static/ — no CDN
    # origins are allowed, so the UI works in air-gapped installs.
    #
    # script-src is nonce-only — strong protection against script injection.
    #
    # style-src uses 'unsafe-inline' WITHOUT a nonce. This is deliberate and
    # required: the Tailwind play-CDN runtime sets inline style= attributes
    # and injects un-nonced <style> elements. Per CSP3, a nonce in style-src
    # makes 'unsafe-inline' be IGNORED — which would block every Tailwind
    # style and leave the whole UI unstyled. Style injection cannot exfiltrate
    # data the way script injection can, and all user content is nh3-
    # sanitised, so an attacker cannot inject a raw <style>/style= anyway.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' {nonce_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self';"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["X-AI-History-Revision"] = _current_revision()
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    if _rate_limit_enabled() and _should_rate_limit(request.path):
        response.headers["X-RateLimit-Limit"] = str(getattr(g, "rate_limit_limit", 0) or 0)
        response.headers["X-RateLimit-Remaining"] = str(getattr(g, "rate_limit_remaining", 0) or 0)
        response.headers["X-RateLimit-Reset"] = str(getattr(g, "rate_limit_reset_in", 0) or 0)

    started_epoch = float(getattr(g, "request_started_epoch", time.time()))
    duration_ms = int((time.time() - started_epoch) * 1000)

    _metrics_inc("requests_total")
    if request.path.startswith("/api/"):
        _metrics_inc("api_requests_total")
    if 200 <= response.status_code < 300:
        _metrics_inc("responses_2xx")
    elif 400 <= response.status_code < 500:
        _metrics_inc("responses_4xx")
    elif response.status_code >= 500:
        _metrics_inc("responses_5xx")

    if request.path.startswith("/api/") or response.status_code >= 400:
        if _json_request_logging_enabled():
            logger.info(
                json.dumps(
                    {
                        "event": "request",
                        "method": request.method,
                        "path": request.path,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                        "request_id": getattr(g, "request_id", ""),
                    },
                    sort_keys=True,
                )
            )
        else:
            logger.info(
                "request method=%s path=%s status=%s duration_ms=%s request_id=%s",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
                getattr(g, "request_id", ""),
            )
    return response


@app.route("/api/build-info")
def api_build_info():
    return jsonify(_build_info_payload())


@app.route("/api/health")
def api_health():
    uptime_seconds = max(0, int(time.time() - APP_STARTED_EPOCH))
    return jsonify(
        {
            "status": "ok",
            "revision": _current_revision(),
            "uptime_seconds": uptime_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )


@app.route("/api/ready")
def api_ready():
    checks: dict[str, Any] = {
        "output_dir_writable": False,
        "index_path_parent_exists": False,
    }
    errors: list[str] = []

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        checks["output_dir_writable"] = OUTPUT_DIR.exists() and os.access(OUTPUT_DIR, os.W_OK)
    except OSError as exc:
        errors.append(f"output_dir_error: {exc}")

    checks["index_path_parent_exists"] = INDEX_PATH.parent.exists()
    checks["index_exists"] = INDEX_PATH.exists()

    ready = bool(checks["output_dir_writable"] and checks["index_path_parent_exists"])
    payload = {
        "ready": ready,
        "status": "ready" if ready else "degraded",
        "revision": _current_revision(),
        "checks": checks,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return jsonify(payload), (200 if ready else 503)


@app.route("/api/metrics")
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
        "revision": _current_revision(),
        "uptime_seconds": uptime_seconds,
        "metrics": metrics_snapshot,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return jsonify(payload)


@app.route("/api/audit")
def api_audit():
    scope = (request.args.get("scope") or "index").strip().lower()
    provider = normalize_tool_name((request.args.get("provider") or "").strip()) or ""
    if provider and not validate_tool_name(provider):
        return jsonify({"error": "Invalid provider"}), 400

    async_mode = (request.args.get("async") or "0").strip() == "1"
    if async_mode:
        job_id = _start_audit_job(scope, provider or None)
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


@app.route("/api/reload-sessions", methods=["POST"])
def api_reload_sessions():
    if (err := _check_local_origin()) is not None:
        return err
    provider = normalize_tool_name((request.args.get("provider") or "").strip()) or ""
    if provider and not validate_tool_name(provider):
        return jsonify({"status": "error", "error": "Invalid provider"}), 400

    full_rebuild = (request.args.get("full") or "0").strip() == "1"
    incremental = not full_rebuild

    async_mode = (request.args.get("async") or "0").strip() == "1"
    if async_mode:
        job_id = _start_reload_job(provider or None, incremental=incremental)
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

    return jsonify(_reload_sessions_index(provider=provider or None, incremental=incremental))


@app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    """Clear all in-memory caches to force reload from disk."""
    if (err := _check_local_origin()) is not None:
        return err
    clear_index_cache()
    clear_sessions_cache()
    return jsonify({"status": "ok", "message": "Cache cleared"})


@app.route("/api/reload-status/<job_id>")
def api_reload_status(job_id):
    state = _get_reload_job(job_id)
    if not state:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(state)


@app.route("/api/audit-status/<job_id>")
def api_audit_status(job_id):
    state = _get_reload_job(job_id)
    if not state or state.get("kind") != "audit":
        return jsonify({"error": "Job not found"}), 404
    return jsonify(state)


@app.route("/api/action-cancel/<job_id>", methods=["POST"])
def api_action_cancel(job_id):
    if (err := _check_local_origin()) is not None:
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


@app.route("/")
def dashboard():
    idx = load_index()
    recent = sorted(
        idx.get("sessions", []),
        key=lambda s: s.get("updated") or s.get("created") or "",
        reverse=True,
    )[:10]
    return render(
        "dashboard",
        active="dashboard",
        stats=idx.get("stats", {}),
        recent_sessions=recent,
        all_tools=list(TOOL_STYLES.keys()),
        title="Dashboard",
    )


# Sessions are rendered server-side one page at a time so the initial HTML
# payload stays small (was up to ~19 MB when every session was inlined).
SESSIONS_PER_PAGE = 50


def _index_mtime():
    """Return the current index mtime used to invalidate session-list caches."""
    try:
        return INDEX_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return None


@threadsafe_lru_cache(maxsize=128)
def _filtered_sorted_session_ids(tool, tag, start, end, index_mtime):
    """Apply list filters and sort newest-first, returning session IDs only."""
    all_s = load_index().get("sessions", [])
    start_dt = parse_date_param(start)
    end_dt = parse_date_param(end)
    filtered = filter_sessions(
        all_s, tool=tool or None, tag=tag or None, start=start_dt, end=end_dt
    )
    filtered = sorted(
        filtered,
        key=lambda s: s.get("updated") or s.get("created") or "",
        reverse=True,
    )
    return tuple(session.get("id") for session in filtered)


def _filtered_sorted_sessions(tool, tag, start, end):
    """Apply list filters and sort newest-first. Returns the full filtered list."""
    session_ids = _filtered_sorted_session_ids(tool, tag, start, end, _index_mtime())
    all_s = load_index().get("sessions", [])
    sessions_by_id = {session.get("id"): session for session in all_s}
    return [
        sessions_by_id[session_id] for session_id in session_ids if session_id in sessions_by_id
    ]


@app.route("/sessions")
def sessions():
    tool = request.args.get("tool", "")
    tag = request.args.get("tag", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")

    if tool and not validate_tool_name(tool):
        return "Invalid tool parameter", 400
    tool = normalize_tool_name(tool) or ""

    if tag and not validate_search_param(tag):
        return "Invalid tag parameter", 400

    filtered = _filtered_sorted_sessions(tool, tag, start, end)
    total = len(filtered)
    pages = max(1, math.ceil(total / SESSIONS_PER_PAGE))
    first_page = filtered[:SESSIONS_PER_PAGE]

    tags = compute_top_tags(load_index().get("sessions", []), limit=12)
    return render(
        "sessions",
        active="sessions",
        sessions=first_page,
        total=total,
        pages=pages,
        per_page=SESSIONS_PER_PAGE,
        tools=list(TOOL_STYLES.keys()),
        tool=tool,
        tag=tag,
        tags=tags,
        start=start,
        end=end,
        back_to=(request.full_path[:-1] if request.full_path.endswith("?") else request.full_path),
        title="Sessions",
    )


@app.route("/sessions/rows")
def sessions_rows():
    """Return an HTML fragment of session rows for one page (lazy 'Load more')."""
    tool = request.args.get("tool", "")
    tag = request.args.get("tag", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")

    if tool and not validate_tool_name(tool):
        return "Invalid tool parameter", 400
    tool = normalize_tool_name(tool) or ""

    if tag and not validate_search_param(tag):
        return "Invalid tag parameter", 400

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return "Invalid page parameter", 400
    if page < 1:
        return "Invalid page parameter", 400

    filtered = _filtered_sorted_sessions(tool, tag, start, end)
    offset = (page - 1) * SESSIONS_PER_PAGE
    page_sessions = filtered[offset : offset + SESSIONS_PER_PAGE]

    html = render("session_rows", sessions=page_sessions, back_to="/sessions")
    return Response(html, mimetype="text/html")


@app.route("/projects")
def projects():
    idx = load_index()
    all_s = idx.get("sessions", [])
    tool = request.args.get("tool", "")
    tag = request.args.get("tag", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")

    if tool and not validate_tool_name(tool):
        return "Invalid tool parameter", 400
    tool = normalize_tool_name(tool) or ""

    if tag and not validate_search_param(tag):
        return "Invalid tag parameter", 400

    start_dt = parse_date_param(start)
    end_dt = parse_date_param(end)
    filtered = filter_sessions(
        all_s, tool=tool or None, tag=tag or None, start=start_dt, end=end_dt
    )

    projects = build_projects_payload(filtered, project_label)
    tags = compute_top_tags(all_s, limit=12)
    return render(
        "projects",
        active="projects",
        projects=projects,
        tools=list(TOOL_STYLES.keys()),
        tool=tool,
        tag=tag,
        tags=tags,
        start=start,
        end=end,
        title="Projects",
    )


def _build_costs_payload() -> dict:
    """Compute token cost stats from the session index."""
    from datetime import timedelta

    idx = load_index()
    sessions = idx.get("sessions", [])

    total_tokens = 0
    session_count = 0
    untokened_count = 0
    by_tool: dict[str, int] = {}
    by_day_map: dict[str, int] = {}
    by_project_map: dict[str, int] = {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()

    for session in sessions:
        raw_tokens = session.get("tokens")
        tokens = 0
        if raw_tokens is not None:
            try:
                tokens = int(raw_tokens)
            except (TypeError, ValueError):
                tokens = 0
        if tokens <= 0:
            # Session exists but carries no usage data (e.g. codex today, or a
            # tool that never reports tokens). Count it so the dashboard can say
            # "N without token data" instead of silently dropping it (#67 follow-up).
            untokened_count += 1
            continue

        total_tokens += tokens
        session_count += 1

        tool = str(session.get("tool") or "unknown")
        by_tool[tool] = by_tool.get(tool, 0) + tokens

        created_raw = str(session.get("created") or "")
        if created_raw:
            try:
                day = datetime.fromisoformat(created_raw[:10]).date()
                if day >= cutoff:
                    day_str = day.isoformat()
                    by_day_map[day_str] = by_day_map.get(day_str, 0) + tokens
            except ValueError:
                pass

        project = str(session.get("project") or "").strip()
        if project:
            by_project_map[project] = by_project_map.get(project, 0) + tokens

    # Sort tool breakdown by tokens descending
    by_tool_sorted = sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)

    # Fill all 30 days (including zeros) for a continuous sparkline
    by_day_list = []
    for i in range(30):
        day = (datetime.now(timezone.utc) - timedelta(days=29 - i)).date()
        by_day_list.append({"date": day.isoformat(), "tokens": by_day_map.get(day.isoformat(), 0)})

    # Top 10 projects by token usage
    by_project_sorted = sorted(by_project_map.items(), key=lambda kv: kv[1], reverse=True)[:10]
    by_project_list = [{"project": p, "tokens": t} for p, t in by_project_sorted]

    return {
        "total_tokens": total_tokens,
        "by_tool": {k: v for k, v in by_tool_sorted},
        "by_day": by_day_list,
        "by_project": by_project_list,
        "session_count": session_count,
        "untokened_count": untokened_count,
    }


@app.route("/api/stats/costs")
def api_stats_costs():
    return jsonify(_build_costs_payload())


@app.route("/stats")
def stats_page():
    """Activity statistics over the last 30 days.

    The flat index carries no token/cost data, so this reports real
    activity — sessions per day/tool/project, message and prompt counts —
    using the same aggregation as the `lore digest` command.
    """
    from datetime import timedelta

    from lore.digest import build_digest

    sessions = load_index().get("sessions", [])
    since = datetime.now() - timedelta(days=30)
    digest = build_digest(sessions, since=since, top_n_projects=10)

    # Continuous 30-day series (fill gaps with 0) for the bar chart.
    by_day_series = []
    for i in range(30):
        day = (datetime.now() - timedelta(days=29 - i)).strftime("%Y-%m-%d")
        by_day_series.append({"date": day, "count": digest.by_day.get(day, 0)})
    peak_day = max((d["count"] for d in by_day_series), default=0)

    return render(
        "stats",
        active="stats",
        total_sessions=digest.total_sessions,
        total_messages=digest.total_messages,
        total_prompts=digest.total_prompts,
        busiest_day=digest.busiest_day,
        by_tool=list(digest.by_tool.items()),
        by_day=by_day_series,
        peak_day=peak_day,
        top_projects=digest.top_projects,
        title="Stats",
    )


def load_session_by_id(
    session_id: str,
    preferred_tool: Optional[str] = None,
    allow_cross_tool_fallback: bool = True,
):
    tools = []
    if preferred_tool:
        tools.append(preferred_tool)
    if allow_cross_tool_fallback:
        tools.extend([tool for tool in TOOL_STYLES.keys() if tool != preferred_tool])
    if not tools:
        return None

    for tool in tools:
        if tool and not validate_tool_name(tool):
            continue
        # Direct single-file lookup first (#127): a cold cache used to pay
        # the full per-tool extraction just to find one session. Falls
        # through to the cached bulk scan when the tool has no
        # deterministic layout or the id is not directly addressable.
        try:
            direct = find_live_session(session_id, tool or None)
        except Exception as exc:
            logger.debug("Direct session lookup failed for %s: %s", tool, exc)
            direct = None
        if direct is not None:
            return direct
        try:
            sessions = load_sessions_for_tool(tool if tool else None)
        except Exception as exc:
            logger.debug("Failed loading sessions for tool %s: %s", tool, exc)
            continue
        for session in sessions:
            if session.session_id == session_id:
                return session
    return None


def _api_limit_param(name: str = "limit", default: int = 20, max_value: int = 200) -> int:
    raw_value = request.args.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise ValueError(f"Invalid {name} parameter")
    if value < 1 or value > max_value:
        raise ValueError(f"Invalid {name} parameter")
    return value


def _index_session_meta(session_id: str) -> Optional[dict[str, Any]]:
    # O(1) dict hit via the payload-identity-keyed memo in the service
    # layer (#125); load_index() stays in the path so test fakes of
    # web.load_index keep driving this lookup.
    return session_by_id_map(load_index()).get(session_id)


# Conversation pairs rendered into the initial session page; the rest are
# fetched lazily via GET /session/<id>/messages?page=N.
MESSAGES_PER_PAGE = 40


@threadsafe_lru_cache(maxsize=16)
def _enriched_session_cached(session_id: str, force_live: bool, _cache_key: str):
    """Stat-keyed cache wrapper around the heavy session build.

    ``_cache_key`` encodes the export file's mtime+size so the cache
    invalidates when the underlying session file changes. maxsize is small
    — these objects are large; we just want a hit when the same session is
    paged through (page 1 → load-more fragments) within seconds.
    """
    return _enriched_session_for_detail_uncached(session_id, force_live=force_live)


def _enriched_session_for_detail(session_id: str, *, force_live: bool):
    """Load + enrich a session by id, cached on the export file's stat.

    A session-detail view pages through one session (page render + several
    "load more" fragment requests). Without caching, each request re-parses
    the entire on-disk session file — for a large session that is multiple
    seconds *per request*. This wraps the build in a stat-keyed cache so the
    parse happens once; ``clear_index_cache()`` does not need to touch it
    because the stat key invalidates it when the file changes.

    Live sessions (no export file) are not cached — their data can change.
    """
    export_path = None
    meta = _index_session_meta(session_id)
    if meta:
        export_path = resolve_export_path(meta.get("export_path"))
    if export_path:
        try:
            st = export_path.stat()
            cache_key = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            cache_key = ""
        if cache_key:
            return _enriched_session_cached(session_id, force_live, cache_key)
    # No export file (live session) or unstat-able — build uncached.
    return _enriched_session_for_detail_uncached(session_id, force_live=force_live)


def _backfill_v2_message_tokens(session_id: str, session_obj) -> None:
    """Copy per-message tokens/model from the v2 store onto a served session (#62).

    The export-markdown served path drops per-message tokens; the v2 store keeps
    them (messages.tokens_json / model). When the v2 message count matches the
    served session's, the rows are in the same seq order (both came from the same
    UnifiedSession at sync time), so tokens/model transfer positionally. On any
    count mismatch we leave the session untouched rather than misalign chips.
    """
    messages = getattr(session_obj, "messages", None)
    if not messages:
        return
    # Only fill what's missing — a live/opencode parse may already carry tokens.
    if any(getattr(m, "tokens", None) for m in messages):
        return
    try:
        from ..storage import load_session_messages_v2

        v2_messages = load_session_messages_v2(OUTPUT_DIR, session_id)
    except Exception as exc:  # storage unavailable / unreadable — non-fatal
        logger.debug("v2 token backfill skipped for %s: %s", session_id, exc)
        return
    if not v2_messages or len(v2_messages) != len(messages):
        return
    for served_msg, v2_msg in zip(messages, v2_messages):
        if v2_msg.tokens and not getattr(served_msg, "tokens", None):
            served_msg.tokens = v2_msg.tokens
        if v2_msg.model and not getattr(served_msg, "model", None):
            served_msg.model = v2_msg.model


def _enriched_session_for_detail_uncached(session_id: str, *, force_live: bool):
    """Load + enrich a session by id for the detail/fragment routes.

    Runs the same multi-path loading logic as ``session_detail`` (export
    markdown → OpenCode JSON → live extraction) and returns a tuple of
    ``(session_obj, toc_items)`` where ``session_obj`` has ``.pairs`` set, or
    ``None`` when no paginatable conversation could be built (the caller then
    falls back to the markdown/summary render paths).
    """
    session_meta = _index_session_meta(session_id)
    if not session_meta:
        return None

    tool_filter = normalize_tool_name(session_meta.get("tool") or "") or session_meta.get("tool")
    export_path = resolve_export_path(session_meta.get("export_path"))
    if not export_path and _export_fallback_scan_enabled():
        export_path = load_export_lookup().get(session_id)
    noise_rules = load_noise_rules()

    def _enrich(session_obj):
        # Backfill per-message tokens/model from the v2 store onto the served
        # session, so chips render without ?live=1 (#62). The export-markdown
        # path drops per-message tokens; the v2 store keeps them.
        if not force_live:
            _backfill_v2_message_tokens(session_id, session_obj)
        toc = enrich_session_for_detail(
            session_obj,
            session_meta,
            format_message_content,
            format_tool_calls,
            format_thinking,
            noise_rules=noise_rules,
        )
        return session_obj, toc

    def _load_preferred_live_session(allow_cross_tool_fallback: bool = False):
        if tool_filter:
            return load_session_by_id(
                session_id,
                preferred_tool=tool_filter,
                allow_cross_tool_fallback=allow_cross_tool_fallback,
            )
        if allow_cross_tool_fallback:
            return load_session_by_id(session_id, allow_cross_tool_fallback=True)
        return None

    if export_path and not force_live:
        parsed_from_md = build_session_from_export_markdown(session_id, session_meta, export_path)
        if parsed_from_md:
            if parsed_from_md.assistant_message_count == 0:
                live_candidate = _load_preferred_live_session(allow_cross_tool_fallback=False)
                if (
                    live_candidate
                    and live_candidate.assistant_message_count
                    > parsed_from_md.assistant_message_count
                ):
                    return _enrich(live_candidate)
            return _enrich(parsed_from_md)

        live_candidate = _load_preferred_live_session(allow_cross_tool_fallback=False)
        if live_candidate:
            return _enrich(live_candidate)
        # No parsable session — caller falls back to raw markdown render.
        return None

    if tool_filter == "opencode":
        try:
            extractor = OpenCodeExtractor(force_full=True)
            candidates = list(extractor.session_path.rglob(f"{session_id}.json"))
            if candidates:
                session_data = extractor._safe_load_json(candidates[0])
                if session_data:
                    parsed = extractor._parse_session(candidates[0], session_data)
                    if parsed:
                        return _enrich(parsed)
        except Exception as exc:
            logger.debug("OpenCode fallback parsing failed for session %s: %s", session_id, exc)

    live_session = None
    if tool_filter:
        live_session = load_session_by_id(
            session_id, preferred_tool=tool_filter, allow_cross_tool_fallback=False
        )
        if not live_session and force_live:
            live_session = load_session_by_id(
                session_id, preferred_tool=tool_filter, allow_cross_tool_fallback=True
            )
    elif force_live:
        live_session = load_session_by_id(session_id, allow_cross_tool_fallback=True)

    if live_session:
        return _enrich(live_session)
    return None


@app.route("/session/<session_id>/messages")
def session_messages_fragment(session_id):
    """Return an HTML fragment of conversation pairs for one page (lazy load)."""
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return "Invalid page parameter", 400
    if page < 1:
        return "Invalid page parameter", 400

    if not _index_session_meta(session_id):
        return "Not found", 404

    force_live = (request.args.get("live") or "").strip() == "1"
    resolved = _enriched_session_for_detail(session_id, force_live=force_live)
    if not resolved:
        return "Not found", 404

    session_obj, _toc = resolved
    pairs = getattr(session_obj, "pairs", []) or []
    offset = (page - 1) * MESSAGES_PER_PAGE
    page_pairs = pairs[offset : offset + MESSAGES_PER_PAGE]

    html_fragment = render(
        "session_pairs",
        pairs=page_pairs,
        pair_offset=offset,
        style=get_style(session_obj.tool.value),
    )
    return Response(html_fragment, mimetype="text/html")


@app.route("/session/<session_id>")
def session_detail(session_id):
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    idx = load_index()
    session_meta = next((s for s in idx.get("sessions", []) if s.get("id") == session_id), None)

    if not session_meta:
        return "Not found", 404

    # Use the index-computed display_title (which already drops low-quality
    # titles like the <local-command-caveat> leak) for the page-title / headline,
    # never the raw, possibly-noisy session.title (#68). A good live title is
    # preferred over this below, when the live extractor provides one.
    clean_title = session_meta.get("display_title") or session_meta.get("title") or "Session"

    back_target = _sanitize_next_url(request.args.get("back", "")) or "/sessions"
    export_path = resolve_export_path(session_meta.get("export_path"))
    if not export_path and _export_fallback_scan_enabled():
        export_path = load_export_lookup().get(session_id)

    def _render_markdown(path_value):
        md_text = Path(path_value).read_text(encoding="utf-8")
        if markdown:
            html_content = markdown.markdown(md_text, extensions=["fenced_code", "tables", "nl2br"])
            html_content = nh3.clean(
                html_content,
                tags=SANITIZE_TAGS,
                attributes=SANITIZE_ATTRS,
                url_schemes=SANITIZE_URL_SCHEMES,
                strip_comments=True,
                link_rel=None,
            )
        else:
            html_content = html.escape(md_text).replace("\n", "<br>")

        return render(
            "rules",
            active="sessions",
            rules=html_content,
            title=clean_title,
        )

    def _render_summary():
        summary = f"""
<h2>{html.escape(clean_title)}</h2>
<p><strong>Tool:</strong> {html.escape(str(session_meta.get("tool") or "n/a"))}</p>
<p><strong>Created:</strong> {html.escape(str(session_meta.get("created") or "n/a"))}</p>
<p><strong>Updated:</strong> {html.escape(str(session_meta.get("updated") or "n/a"))}</p>
<p><strong>Messages:</strong> {html.escape(str(session_meta.get("messages") or 0))} | <strong>Prompts:</strong> {html.escape(str(session_meta.get("prompts") or 0))}</p>
<p><strong>Project:</strong> {html.escape(str(session_meta.get("project") or "Unassigned"))}</p>
<hr>
<p>This session has no linked export markdown yet. Open full live transcript with <a href="?live=1"><code>?live=1</code></a>, or run <code>lore sync &lt;tool&gt;</code> to attach export paths.</p>
<p><strong>Prompt Outline:</strong><br>{html.escape(str(session_meta.get("prompt_outline") or "n/a"))}</p>
"""
        return render(
            "rules",
            active="sessions",
            rules=summary,
            title=clean_title,
        )

    force_live = (request.args.get("live") or "").strip() == "1"

    resolved = _enriched_session_for_detail(session_id, force_live=force_live)
    if resolved:
        session_obj, toc = resolved
        # The <h1> headline reads session.title directly. Only replace it with the
        # cleaned title when the object's own title is missing or low-quality (e.g.
        # the <local-command-caveat> leak) — a good live-extractor title wins (#68).
        from ..exporters.index import is_low_quality_title

        obj_title = (session_obj.title or "").strip()
        if not obj_title or is_low_quality_title(obj_title):
            session_obj.title = clean_title
        pairs = getattr(session_obj, "pairs", []) or []
        total_pairs = len(pairs)
        message_pages = max(1, math.ceil(total_pairs / MESSAGES_PER_PAGE))
        visible_pairs = pairs[:MESSAGES_PER_PAGE]
        return render(
            "session",
            active="sessions",
            session=session_obj,
            visible_pairs=visible_pairs,
            total_pairs=total_pairs,
            message_pages=message_pages,
            messages_per_page=MESSAGES_PER_PAGE,
            style=get_style(session_obj.tool.value),
            title=session_obj.title or clean_title,
            toc_items=toc,
            back_target=back_target,
        )

    # No paginatable conversation — fall back to raw markdown or a summary card.
    if export_path and not force_live:
        try:
            return _render_markdown(export_path)
        except OSError as exc:
            logger.debug("Failed to render export markdown for session %s: %s", session_id, exc)

    return _render_summary()


@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_session_resume(session_id):
    if (err := _check_local_origin()) is not None:
        return err
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    tool = str(session_meta.get("tool") or "").strip()
    project_path = str(session_meta.get("project") or "").strip()

    if tool == "claude-code":
        command = f"claude --resume {session_id}"
        if project_path:
            command = f"cd {project_path} && {command}"
        return jsonify(
            {
                "supported": True,
                "command": command,
                "project": project_path or None,
                "tool": tool,
            }
        )
    elif tool == "opencode":
        command = f"opencode --resume {session_id}"
        if project_path:
            command = f"cd {project_path} && {command}"
        return jsonify(
            {
                "supported": True,
                "command": command,
                "project": project_path or None,
                "tool": tool,
            }
        )
    elif tool in ("cursor", "vscode", "vs-code"):
        return jsonify(
            {
                "supported": False,
                "command": None,
                "project": project_path or None,
                "tool": tool,
                "reason": "Cannot resume Cursor/VS Code sessions programmatically",
            }
        )
    else:
        return jsonify(
            {
                "supported": False,
                "command": None,
                "project": project_path or None,
                "tool": tool,
                "reason": f"Resume not supported for tool: {tool or 'unknown'}",
            }
        )


@app.route("/api/sessions/<session_id>/tags", methods=["GET", "POST", "DELETE"])
def api_session_tags(session_id):
    """User-editable tags / bookmarks for a session (#56).

    GET    → {"tags": [...]}
    POST   {"tag": "x"}  → add, returns updated {"tags": [...]}
    DELETE {"tag": "x"}  → remove, returns updated {"tags": [...]}
    """
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    from ..storage import add_session_tag, get_session_tags, remove_session_tag

    if request.method == "GET":
        return jsonify({"tags": get_session_tags(OUTPUT_DIR, session_id)})

    # Mutations are local-origin only, like the other POST/DELETE endpoints.
    if (err := _check_local_origin()) is not None:
        return err

    payload = request.get_json(silent=True)
    tag = (payload or {}).get("tag") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        return jsonify({"error": "a non-empty 'tag' is required"}), 400

    try:
        if request.method == "POST":
            tags = add_session_tag(OUTPUT_DIR, session_id, tag)
        else:  # DELETE
            tags = remove_session_tag(OUTPUT_DIR, session_id, tag)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    clear_index_cache()
    return jsonify({"tags": tags})


@app.route("/api/tags")
def api_all_tags():
    """All user tags with session counts: {"tags": {tag: count, ...}}."""
    from ..storage import list_all_tags

    return jsonify({"tags": list_all_tags(OUTPUT_DIR)})


@app.route("/session/<session_id>/delete", methods=["POST"])
def session_delete(session_id):
    if (err := _check_local_origin()) is not None:
        return err
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    # Find the session through load_index() so this works regardless of
    # whether the active store is v2 or the legacy JSON index (#44).
    sessions = load_index().get("sessions", [])
    session_meta = next((s for s in sessions if s.get("id") == session_id), None)

    if not session_meta:
        return "Not found", 404

    export_path_str = session_meta.get("export_path")
    if export_path_str:
        export_path = resolve_export_path(export_path_str)
        if export_path and export_path.exists():
            try:
                export_path.unlink()
            except OSError:
                pass

    # Deletion is recorded as a tombstone — both the v2 reader and the JSON
    # reader filter tombstoned ids out. No need to rewrite any index in
    # place (the in-place index.json rewrite was dead code on the v2 path).
    remember_deleted_session_id(session_id)
    clear_index_cache()

    next_url = _sanitize_next_url(request.form.get("next", ""))
    if not next_url:
        referer = request.headers.get("Referer", "")
        parsed = urlparse(referer)
        if not parsed.netloc or parsed.netloc == request.host:
            candidate = (parsed.path or "") + (("?" + parsed.query) if parsed.query else "")
            next_url = _sanitize_next_url(candidate)

    return redirect(next_url or "/sessions")


@app.route("/threads")
def threads():
    idx = load_index()
    threads_sorted = build_threads_overview(idx.get("sessions", []))
    return render("threads", active="threads", threads=threads_sorted, title="Threads")


@app.route("/thread/<thread_id>")
def thread_detail(thread_id):
    if not validate_session_id(thread_id):
        return "Invalid thread id", 400

    idx = load_index()
    payload = build_thread_detail_payload(
        thread_id,
        idx.get("sessions", []),
        load_sessions_for_tool,
        format_message_content,
        format_tool_calls,
        get_style,
        noise_rules=load_noise_rules(),
        sanitize_rendered_html_fn=sanitize_rendered_html,
    )
    if not payload["thread_timeline"]:
        return "Not found", 404

    return render(
        "thread_detail",
        active="threads",
        thread_id=thread_id,
        messages=payload["messages"],
        toc_items=payload["toc_items"],
        continue_cmd=payload["continue_cmd"],
        thread_meta=payload["thread_meta"],
        thread_groups=payload["thread_groups"],
        thread_timeline=payload["thread_timeline"],
        title="Thread",
    )


@app.route("/rules")
def rules():
    rules_path = OUTPUT_DIR / "rules.md"
    rules_text = ""
    if rules_path.exists():
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_text = f.read()
    if rules_text and markdown:
        rules_text = markdown.markdown(rules_text, extensions=["fenced_code", "tables", "nl2br"])
        rules_text = nh3.clean(
            rules_text,
            tags=SANITIZE_TAGS,
            attributes=SANITIZE_ATTRS,
            url_schemes=SANITIZE_URL_SCHEMES,
            strip_comments=True,
            link_rel=None,
        )
    elif rules_text:
        rules_text = html.escape(rules_text).replace("\n", "<br>")
    return render("rules", active="rules", rules=rules_text, title="Rules")


@app.route("/memory")
def memory_page():
    """Browse and search the shared agent-memory store (#33)."""
    from lore.storage import (
        MEMORY_KINDS,
        embeddings_available,
        list_memory,
        list_memory_sources,
        recall_memory,
    )

    query = request.args.get("q", "").strip()
    kind = request.args.get("kind", "").strip() or None
    semantic = request.args.get("semantic", "") in ("1", "true", "on")

    if query and not validate_search_param(query):
        return "Invalid search query", 400
    if kind and kind not in MEMORY_KINDS:
        return "Invalid kind", 400

    output_dir = INDEX_PATH.parent
    if query:
        entries = recall_memory(output_dir, query, kind=kind, limit=100, semantic=semantic)
    else:
        entries = list_memory(output_dir, kind=kind, limit=100)

    # Attach source-session provenance to each memory (#33).
    memories = []
    for entry in entries:
        record = entry.to_dict()
        record["sources"] = list_memory_sources(output_dir, entry.id)
        memories.append(record)

    return render(
        "memory",
        active="memory",
        memories=memories,
        total=len(entries),
        query=query,
        kind=kind,
        kinds=list(MEMORY_KINDS),
        semantic=semantic,
        semantic_available=embeddings_available(),
        title="Memory",
    )


@app.route("/memory/<int:memory_id>/delete", methods=["POST"])
def memory_delete(memory_id):
    if (err := _check_local_origin()) is not None:
        return err
    from lore.storage import delete_memory

    delete_memory(INDEX_PATH.parent, memory_id)
    return redirect("/memory")


@app.route("/noise-rules")
def noise_rules_page():
    return render(
        "noise_rules",
        active="noise-rules",
        noise_rules=load_noise_rules(),
        noise_rule_presets=_noise_rule_presets(),
        title="Noise Rules",
    )


@app.route("/api/noise-rules", methods=["GET", "POST"])
def api_noise_rules():
    if request.method == "POST":
        if (err := _check_local_origin()) is not None:
            return err
    if request.method == "GET":
        return jsonify(load_noise_rules())

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload"}), 400
    rules = save_noise_rules(payload)
    clear_sessions_cache()
    clear_index_cache()
    return jsonify({"status": "ok", "rules": rules})


@app.route("/api/noise-rules/preview", methods=["POST"])
def api_noise_rules_preview():
    if (err := _check_local_origin()) is not None:
        return err
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload"}), 400

    tool = normalize_tool_name(str(payload.get("tool") or "").strip()) or ""
    role = str(payload.get("role") or "assistant").strip().lower()
    content = str(payload.get("content") or "")
    provided_rules = payload.get("noise_rules")
    if provided_rules is None:
        rules = load_noise_rules()
    elif isinstance(provided_rules, dict):
        merged = load_noise_rules()
        for provider, provider_rules in provided_rules.items():
            if not isinstance(provider, str) or not isinstance(provider_rules, dict):
                continue
            dst = merged.setdefault(provider, {})
            for key, value in provider_rules.items():
                if isinstance(key, str) and isinstance(value, bool):
                    dst[key] = value
        rules = merged
    else:
        return jsonify({"error": "Invalid noise_rules"}), 400

    normalized = preview_normalize_message(
        tool=tool,
        role=role,
        content=content,
        noise_rules=rules,
    )
    return jsonify({"content": normalized, "tool": tool, "role": role})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if len(q) < 2:
        return jsonify([])

    if not validate_search_param(q):
        return jsonify({"error": "Invalid search query"}), 400

    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    project = request.args.get("project", "")
    if project and not validate_search_param(project):
        return jsonify({"error": "Invalid project parameter"}), 400

    res = search_index(q, tool=tool or None, project=project or None, limit=10)
    return jsonify(
        [
            {
                "id": r["session"].get("id"),
                "title": r["session"].get("title"),
                "tool": r["session"].get("tool"),
                "date": r["session"].get("created")[:10],
            }
            for r in res
        ]
    )


@app.route("/api/v1/search")
def api_v1_search():
    q = request.args.get("q", "")
    if len(q) < 2:
        return jsonify({"query": q, "count": 0, "results": []})

    if not validate_search_param(q):
        return jsonify({"error": "Invalid search query"}), 400

    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    project = request.args.get("project", "")
    if project and not validate_search_param(project):
        return jsonify({"error": "Invalid project parameter"}), 400

    # Message-role scope (#97) — was silently ignored before; validate like the
    # MCP search_history tool does and reject unknown values instead of lying.
    from lore.storage.search import SEARCH_SCOPES

    scope = (request.args.get("scope") or "all").strip().lower()
    if scope not in SEARCH_SCOPES:
        return (
            jsonify(
                {"error": f"Invalid scope. Expected one of: {', '.join(sorted(SEARCH_SCOPES))}"}
            ),
            400,
        )

    try:
        limit = _api_limit_param(default=20, max_value=200)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    results = search_index(q, tool=tool or None, project=project or None, limit=limit, scope=scope)
    return jsonify(
        {
            "query": q,
            "tool": tool or None,
            "project": project or None,
            "scope": scope,
            "count": len(results),
            "results": [
                {
                    **serialize_index_session_summary(result["session"]),
                    "score": result.get("score"),
                }
                for result in results
            ],
        }
    )


def _api_page_params(
    default_per_page: int = 50,
    max_per_page: int = 500,
) -> tuple[int, int]:
    """Parse and validate ?page= and ?per_page= query parameters.

    Returns (page, per_page) where page is 1-based.
    Raises ValueError with a descriptive message on bad input.
    """
    raw_page = request.args.get("page", "1").strip()
    raw_per_page = request.args.get("per_page", str(default_per_page)).strip()
    try:
        page = int(raw_page)
    except ValueError:
        raise ValueError("Invalid page parameter")
    try:
        per_page = int(raw_per_page)
    except ValueError:
        raise ValueError("Invalid per_page parameter")
    if page < 1:
        raise ValueError("Invalid page parameter")
    if per_page < 1 or per_page > max_per_page:
        raise ValueError(f"per_page must be between 1 and {max_per_page}")
    return page, per_page


@app.route("/api/v1/index/summary")
def api_v1_index_summary():
    """Return lightweight index metadata without loading full session records.

    Useful for dashboard initialisation — fetch this first (fast), then
    paginate sessions lazily via GET /api/v1/sessions?page=N&per_page=M.
    """
    summary = load_index_summary()
    return jsonify(summary)


@app.route("/api/v1/sessions")
def api_v1_sessions():
    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    project = request.args.get("project", "")
    if project and not validate_search_param(project):
        return jsonify({"error": "Invalid project parameter"}), 400

    thread_id = request.args.get("thread_id", "")
    if thread_id and not validate_session_id(thread_id):
        return jsonify({"error": "Invalid thread_id parameter"}), 400

    q = request.args.get("q", "")
    if q and not validate_search_param(q):
        return jsonify({"error": "Invalid search query"}), 400

    # Pagination: ?page=1&per_page=50 (preferred) or legacy ?limit= (still honoured
    # when page/per_page are absent so existing integrations keep working).
    use_pagination = "page" in request.args or "per_page" in request.args
    if use_pagination:
        try:
            page, per_page = _api_page_params(default_per_page=50, max_per_page=500)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        limit = None  # will slice via pagination below
    else:
        try:
            limit = _api_limit_param(default=50, max_value=500)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        page, per_page = 1, limit

    if len(q) >= 2:
        search_limit = (page * per_page) if use_pagination else (limit or 500)
        all_sessions = [
            result["session"]
            for result in search_index(
                q, tool=tool or None, project=project or None, limit=search_limit
            )
        ]
    else:
        all_sessions = load_index().get("sessions", [])
        if tool:
            all_sessions = [s for s in all_sessions if s.get("tool") == tool]
        if project:
            all_sessions = [s for s in all_sessions if project in str(s.get("project") or "")]
        if thread_id:
            all_sessions = [s for s in all_sessions if str(s.get("thread_id") or "") == thread_id]
        all_sessions = sorted(
            all_sessions,
            key=lambda s: str(s.get("updated") or s.get("created") or ""),
            reverse=True,
        )

    total = len(all_sessions)

    if use_pagination:
        offset = (page - 1) * per_page
        page_sessions = all_sessions[offset : offset + per_page]
        pages = math.ceil(total / per_page) if per_page else 1
        return jsonify(
            {
                "sessions": [serialize_index_session_summary(s) for s in page_sessions],
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": pages,
            }
        )
    else:
        sessions = all_sessions[:limit]
        return jsonify(
            {
                "count": len(sessions),
                "sessions": [serialize_index_session_summary(s) for s in sessions],
            }
        )


@app.route("/api/v1/sessions/<session_id>")
def api_v1_session_detail(session_id):
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    live = request.args.get("live", "0") == "1"
    preferred_tool = normalize_tool_name(session_meta.get("tool") or "") or None
    session_obj = load_session_by_id(
        session_id,
        preferred_tool=preferred_tool,
        allow_cross_tool_fallback=live,
    )

    if session_obj:
        return jsonify(serialize_live_session(session_obj, session_meta=session_meta))

    payload = serialize_index_session_summary(session_meta)
    payload["live"] = False
    return jsonify(payload)


@app.route("/api/v1/sessions/<session_id>/messages")
def api_v1_session_messages(session_id):
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    try:
        limit = _api_limit_param(default=500, max_value=5000)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    preferred_tool = normalize_tool_name(session_meta.get("tool") or "") or None
    session_obj = load_session_by_id(
        session_id,
        preferred_tool=preferred_tool,
        allow_cross_tool_fallback=True,
    )
    if not session_obj:
        return jsonify(
            {
                "session_id": session_id,
                "message_count": 0,
                "messages": [],
                "live": False,
            }
        )

    payload = serialize_live_session(
        session_obj,
        session_meta=session_meta,
        include_messages=True,
    )
    payload["messages"] = payload["messages"][:limit]
    payload["message_count"] = len(payload["messages"])
    return jsonify(payload)


@app.route("/api/v1/projects")
def api_v1_projects():
    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    try:
        limit = _api_limit_param(default=100, max_value=500)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    sessions = load_index().get("sessions", [])
    if tool:
        sessions = [session for session in sessions if session.get("tool") == tool]
    projects = build_projects_payload(sessions, project_label)
    return jsonify(
        {
            "count": min(len(projects), limit),
            "projects": [serialize_project(project) for project in projects[:limit]],
        }
    )


@app.route("/api/v1/threads")
def api_v1_threads():
    try:
        limit = _api_limit_param(default=100, max_value=500)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    threads = build_threads_overview(load_index().get("sessions", []))[:limit]
    return jsonify(
        {
            "count": len(threads),
            "threads": [serialize_thread_overview(thread) for thread in threads],
        }
    )


@app.route("/api/v1/threads/<thread_id>")
def api_v1_thread_detail(thread_id):
    if not validate_session_id(thread_id):
        return jsonify({"error": "Invalid thread id"}), 400

    payload = build_thread_detail_payload(
        thread_id,
        load_index().get("sessions", []),
        load_sessions_for_tool,
        format_message_content,
        format_tool_calls,
        get_style,
        noise_rules=load_noise_rules(),
        sanitize_rendered_html_fn=sanitize_rendered_html,
    )
    if not payload["thread_timeline"]:
        return jsonify({"error": "Not found"}), 404

    return jsonify(
        {
            "thread": {
                "id": thread_id,
                **payload["thread_meta"],
            },
            "timeline": [
                serialize_index_session_summary(session) for session in payload["thread_timeline"]
            ],
            "messages": serialize_thread_messages(payload["messages"]),
            "toc_items": payload["toc_items"],
            "groups": payload["thread_groups"],
            "continue_command": payload["continue_cmd"],
        }
    )


@app.route("/export/index")
def export_index():
    if not INDEX_PATH.exists():
        return "Index not found", 404
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        data = f.read()
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="index.json"'},
    )


@app.route("/export/<session_id>")
def export_session(session_id):
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    idx = load_index()
    session_meta = next((s for s in idx.get("sessions", []) if s.get("id") == session_id), None)
    from ..exporters.markdown import MarkdownExporter

    if session_meta:
        export_path = resolve_export_path(session_meta.get("export_path"))
        if export_path and export_path.exists():
            return Response(
                export_path.read_text(encoding="utf-8", errors="replace"),
                mimetype="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{export_path.name}"'},
            )

        tool_filter = normalize_tool_name(session_meta.get("tool") or "")
        if tool_filter:
            session = load_session_by_id(session_id, preferred_tool=tool_filter)
            if session:
                return Response(
                    MarkdownExporter(OUTPUT_DIR)._generate_markdown(session),
                    mimetype="text/markdown",
                )

    if os.environ.get("LORE_EXPORT_FALLBACK_SCAN", "").lower() != "true":
        return "Not found", 404

    for ex in get_all_extractors():
        if not ex.is_available():
            continue
        for session in ex.extract_sessions():
            if session.session_id == session_id:
                return Response(
                    MarkdownExporter(OUTPUT_DIR)._generate_markdown(session),
                    mimetype="text/markdown",
                )
    return "Not found", 404


def start_web_ui(port=5000, host="127.0.0.1", debug=False):
    clear_index_cache()
    clear_sessions_cache()
    logger.info("Cache cleared on startup")
    app.run(host=host, port=port, debug=debug)
