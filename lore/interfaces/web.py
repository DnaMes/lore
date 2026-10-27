import html
import importlib
import json
import logging
import os
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import markdown
import nh3
from flask import Flask, Response, g, has_request_context, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from lore.extractors.factory import get_all_extractors
from lore.utils.formatting import escape_snippet_html
from lore.utils.text_processing import format_thinking

from ..services.extraction import find_live_session
from ..services.index import (
    InvalidFlagError,
    automated_hidden_line,
    filter_automated_payload,
    is_automated,
    load_deleted_session_ids,
    parse_bool_flag,
    thread_hidden_count,
    thread_sessions,
)

# Audit + build-info payload builders moved verbatim to web_audit.py (#119).
# Re-exported (PEP 484 redundant aliases) because web_jobs resolves them via
# late `from .web import ...` lookups and the test suite patches them here —
# the module-global bindings must keep existing.
from .fast_bind import skip_reverse_dns_on_bind
from .web_audit import (  # noqa: F401  (re-exports, see comment)
    _audit_index_sessions as _audit_index_sessions,
)
from .web_audit import (
    _audit_live_sessions as _audit_live_sessions,
)
from .web_audit import (
    _build_info_payload as _build_info_payload,
)
from .web_audit import (
    _finalize_audit_payload as _finalize_audit_payload,
)
from .web_audit import (
    _new_tool_audit_row as _new_tool_audit_row,
)
from .web_data import (
    DELETED_SESSIONS_PATH,
    INDEX_PATH,
    OUTPUT_DIR,
    clear_index_cache,
    clear_sessions_cache,
    load_index,
    load_index_summary,
    load_sessions_for_tool,
    resolve_export_path,
    search_index,
)
from .web_formatting import (
    SANITIZE_ATTRS,
    SANITIZE_TAGS,
    SANITIZE_URL_SCHEMES,
    format_compact_token_count,
    format_message_content,
    format_session_duration,
    format_session_row_time,
    format_tool_calls,
    is_automated_origin,
    render_markdown,
    session_row_tokens,
    should_show_prompt_outline,
)
from .web_helpers import (
    TOOL_STYLES,
    filter_sessions,
    get_style,
    project_label,
)
from .web_jobs import (
    ACTION_JOB_TIMEOUT_SECONDS,
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
from .web_summary import (
    build_summary_card_context,
)
from .web_summary import (
    cite_href as summary_cite_href,
)
from .web_utils import (
    METRICS,
    METRICS_LOCK,
    NOISE_RULES_PATH,
    RATE_LIMIT_STATE,
    ActionJobTimeoutError,
    _client_ip,
    _consume_rate_limit,
    _json_request_logging_enabled,
    _metrics_inc,
    _metrics_snapshot,
    _rate_limit_enabled,
    _record_job_outcome,
    _request_id,
    _should_rate_limit,
    forwarded_header_error,
    host_header_allowed,
    load_noise_rules,
    request_is_cross_origin,
    trusted_proxy_enabled,
)

_TEST_EXPORTS = (
    DELETED_SESSIONS_PATH,
    INDEX_PATH,
    find_live_session,
    get_all_extractors,
    _metrics_snapshot,
    filter_sessions,
    format_message_content,
    format_thinking,
    format_tool_calls,
    load_index_summary,
    load_noise_rules,
    load_sessions_for_tool,
    resolve_export_path,
    search_index,
    ACTION_JOB_TIMEOUT_SECONDS,
    RELOAD_JOB_MAX,
    RELOAD_JOB_TTL_SECONDS,
    RELOAD_JOBS,
    RELOAD_JOBS_LOCK,
    _assert_job_active,
    _cancel_action_job,
    _get_reload_job,
    _prune_reload_jobs_locked,
    _run_audit,
    _start_audit_job,
    _start_reload_job,
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
# Set TRUSTED_PROXY=1 (or true/yes/on) when a proxy sits in front.
if trusted_proxy_enabled():
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]


_SESSION_WATCHER = None

# Bind host passed to ``start_web_ui``; part of the Host allowlist. Gunicorn
# deployments never call it and rely on ``$LORE_WEB_HOST`` instead.
_BIND_HOST = ""


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

    Accepted origins:

    - the request's own origin — ``scheme://host[:port]`` exactly as the
      connection scheme and the ``Host`` header spell it. A loopback Origin on
      a different port is no longer a free pass: another local app is
      cross-origin like any other site (follow-up to #193),
    - an origin listed in ``LORE_PUBLIC_ORIGINS`` — but only while
      ``TRUSTED_PROXY`` is set, because without a proxy nothing rewrites
      scheme/host and the configured list would just be attack surface.

    Scheme *and* port are part of the comparison, so ``https://host:8443``
    never authorises ``http://host`` or ``https://host``. Malformed
    ``X-Forwarded-*`` headers are rejected in ``before_request`` first: a
    request whose forwarded headers do not parse is not one a proxy sent. The
    policy itself is :func:`lore.interfaces.web_utils.request_is_cross_origin`,
    shared with the MCP HTTP transport.
    """
    if not request_is_cross_origin():
        return None
    return jsonify({"error": "Cross-origin request rejected"}), 403


def _current_revision() -> str:
    return os.environ.get("LORE_BUILD_SHA") or os.environ.get("LORE_BUILD_REVISION") or "dev"


def _export_fallback_scan_enabled() -> bool:
    return os.environ.get("LORE_EXPORT_FALLBACK_SCAN", "").lower() == "true"


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
    env.filters["snippet_html"] = escape_snippet_html
    return env


def _include_automated() -> bool:
    """True when the request asks for automated sessions (``?automated=1``).

    A repeated parameter (``?automated=1&automated=0``) takes its first value; an
    unknown value raises ``InvalidFlagError`` (answered as HTTP 400).
    """
    if not has_request_context():
        return False
    return parse_bool_flag(request.args.get("automated"), name="automated")


@app.errorhandler(InvalidFlagError)
def _invalid_flag(exc: InvalidFlagError):
    """A boolean query parameter with an unknown value is a plain 400 JSON error."""
    return jsonify({"error": str(exc)}), 400


def _automated_kwargs() -> dict:
    """``include_automated=True`` only when asked, so wrapped/faked callables stay compatible."""
    return {"include_automated": True} if _include_automated() else {}


def _visible_index() -> dict:
    """The index without automated sessions (unless ``?automated=1``).

    ``load_index()`` stays unfiltered: lookups by id (session pages, export, delete,
    sync merge) must keep working for hidden sessions. Only list views use this.
    """
    return filter_automated_payload(load_index(), _include_automated())


def _thread_sessions(thread_id: str) -> list:
    """Index sessions a thread view works on; a known thread id never turns into a 404."""
    return thread_sessions(load_index().get("sessions", []), thread_id, _include_automated())


def _thread_hidden(thread_id: str) -> int:
    """Automated children of this thread the default view leaves out."""
    return thread_hidden_count(load_index().get("sessions", []), thread_id, _include_automated())


def _origin_scoped(rows: list, tag: str, hidden: Optional[list] = None) -> list:
    """List-view sessions after the origin filter.

    Filtering by a tag skips the origin filter: a session carrying that tag must stay
    visible even when it is automated. ``hidden`` receives the number of sessions left out.
    """
    if tag or _include_automated():
        return rows
    visible = [s for s in rows if not is_automated(s)]
    if hidden is not None:
        hidden.append(len(rows) - len(visible))
    return visible


def render(tpl_name, **kwargs):
    env = _template_env()
    idx = _visible_index()
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
    if tpl_name == "session" and "summary_mode" not in kwargs:
        session_obj = kwargs.get("session")
        session_id = None
        if isinstance(session_obj, dict):
            session_id = session_obj.get("session_id") or session_obj.get("id")
        elif session_obj is not None:
            session_id = getattr(session_obj, "session_id", None)
        if session_id:
            pairs = kwargs.get("visible_pairs")
            if pairs is None:
                if isinstance(session_obj, dict):
                    pairs = session_obj.get("pairs") or []
                elif session_obj is not None:
                    pairs = getattr(session_obj, "pairs", []) or []
            deleted_ids = load_deleted_session_ids(DELETED_SESSIONS_PATH)
            kwargs.update(
                build_summary_card_context(
                    OUTPUT_DIR,
                    str(session_id),
                    session_obj,
                    pairs or [],
                    session_deleted=str(session_id) in deleted_ids,
                )
            )
    from lore import __version__

    include_automated = _include_automated()
    # A view that counted within its own filters passes ``automated_hidden``.
    hidden_count = kwargs.pop("automated_hidden", None)
    if hidden_count is None:
        hidden_count = idx.get("automated_hidden") or 0
    hidden_line = automated_hidden_line(int(hidden_count))
    show_url = ""
    if hidden_line and has_request_context():
        show_url = request.path + "?" + urlencode({**request.args.to_dict(), "automated": "1"})
    kwargs.setdefault("include_automated", include_automated)
    kwargs.setdefault("automated_hidden_line", hidden_line)
    kwargs.setdefault("automated_show_url", show_url)
    if "shortcuts_page" not in kwargs:
        shortcuts_pages = {"sessions": "list", "session": "detail"}
        if tpl_name in shortcuts_pages:
            kwargs["shortcuts_page"] = shortcuts_pages[tpl_name]

    return env.get_template(f"{tpl_name}.html").render(
        get_style=get_style,
        project_label=project_label,
        format_compact_token_count=format_compact_token_count,
        format_session_duration=format_session_duration,
        format_session_row_time=format_session_row_time,
        is_automated_origin=is_automated_origin,
        session_row_tokens=session_row_tokens,
        should_show_prompt_outline=should_show_prompt_outline,
        sessions_query_url=sessions_query_url,
        summary_cite_href=summary_cite_href,
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

    # #193: while a proxy is supposedly in front, malformed X-Forwarded-*
    # headers mean no proxy sent this request. Reject before anything parses
    # scheme/host — the Host allowlist below sees the ProxyFix-rewritten host.
    if (bad := forwarded_header_error()) is not None:
        logger.warning(
            "Rejecting request with bad forwarded headers: %s request_id=%s", bad, g.request_id
        )
        return jsonify({"error": "Invalid forwarded headers"}), 400

    # Host allowlist: a request naming a foreign domain (DNS rebinding,
    # poisoned Host) never reaches a route. Loopback names, the bind host and
    # operator-configured hosts (LORE_ALLOWED_HOSTS, LORE_PUBLIC_ORIGINS) pass.
    if not host_header_allowed(request.headers.get("Host", ""), _BIND_HOST):
        _metrics_inc("host_rejections")
        logger.warning(
            "Rejecting request with disallowed Host header: %r request_id=%s",
            request.headers.get("Host", "")[:120],
            g.request_id,
        )
        return Response(
            "Bad Host header. Allowed hosts can be configured via LORE_ALLOWED_HOSTS.",
            status=400,
            mimetype="text/plain",
        )

    # Mutating requests: when an Origin header is present it must spell this
    # request's own scheme://host[:port] or a configured public origin exactly
    # — a loopback Origin on a different port is cross-origin like any other
    # site (follow-up to #193). A missing Origin keeps the historical
    # behaviour: curl and MCP clients never send one. The per-route
    # _check_local_origin() guards remain as defence in depth.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin_err = _check_local_origin()
        if origin_err is not None:
            return origin_err

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
    # origins are allowed, so the UI works in air-gapped installs. Tailwind is
    # a build-time compiled stylesheet (#129), not a browser-side JIT runtime.
    #
    # script-src is nonce-only — strong protection against script injection.
    #
    # style-src uses 'unsafe-inline' WITHOUT a nonce. This is deliberate and
    # required: templates and their inline scripts set style= attributes and
    # <style> blocks directly (code-block wrappers, toast positioning, etc.).
    # Per CSP3, a nonce in style-src makes 'unsafe-inline' be IGNORED — which
    # would block every inline style and leave parts of the UI unstyled.
    # Style injection cannot exfiltrate data the way script injection can,
    # and all user content is nh3-sanitised, so an attacker cannot inject a
    # raw <style>/style= anyway.
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
    response.headers["X-Lore-Revision"] = _current_revision()
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    peek_max_age = getattr(g, "peek_cache_max_age", None)
    if peek_max_age is not None:
        response.headers["Cache-Control"] = f"private, max-age={int(peek_max_age)}"
        response.headers.pop("Pragma", None)
        response.headers.pop("Expires", None)
    else:
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


# payload stays small (was up to ~19 MB when every session was inlined).


# fetched lazily via GET /session/<id>/messages?page=N.


@app.route("/rules")
def rules():
    rules_path = OUTPUT_DIR / "rules.md"
    rules_text = ""
    if rules_path.exists():
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_text = f.read()
    if rules_text and markdown:
        rules_text = render_markdown(rules_text)
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


def start_web_ui(port=5000, host="127.0.0.1", debug=False):
    global _BIND_HOST
    _BIND_HOST = host
    clear_index_cache()
    clear_sessions_cache()
    logger.info("Cache cleared on startup")
    skip_reverse_dns_on_bind()  # a reverse lookup made macOS start ~35 s late
    app.run(host=host, port=port, debug=debug)


# --- Route blueprints (#119) --------------------------------------------------
# Handlers live in ``lore/interfaces/routes/`` and are registered on ``app`` at
# the end of this module. They keep their old module-level names here so
# ``lore.interfaces.web.<name>`` stays a valid patch target; the routes resolve
# those seams through this module at call time.
from .routes import register_blueprints  # noqa: E402
from .routes.api_v1 import (  # noqa: E402,F401  (re-exports)
    _api_limit_param,
    _api_page_params,
    _bounded_message_window,
    api_v1_index_summary,
    api_v1_message_permalink,
    api_v1_projects,
    api_v1_search,
    api_v1_session_detail,
    api_v1_session_messages,
    api_v1_sessions,
    api_v1_thread_detail,
    api_v1_threads,
)
from .routes.jobs import (  # noqa: E402,F401  (re-exports)
    _reload_sessions_index,
    api_action_cancel,
    api_audit,
    api_audit_status,
    api_build_info,
    api_clear_cache,
    api_health,
    api_metrics,
    api_ready,
    api_reload_sessions,
    api_reload_status,
)
from .routes.memory import (  # noqa: E402,F401  (re-exports)
    api_noise_rules,
    api_noise_rules_preview,
    memory_delete,
    memory_page,
    memory_review,
    noise_rules_page,
)
from .routes.search import (  # noqa: E402,F401  (re-exports)
    _search_filter_params,
    api_search,
)
from .routes.sessions import (  # noqa: E402,F401  (re-exports)
    _SUMMARY_FORBIDDEN_PARAMS,
    BULK_SESSION_ACTION_LIMIT,
    MESSAGES_PER_PAGE,
    SESSIONS_PER_PAGE,
    _backfill_v2_message_tokens,
    _build_costs_payload,
    _enriched_session_cached,
    _enriched_session_for_detail,
    _enriched_session_for_detail_uncached,
    _filtered_automated_hidden,
    _filtered_sorted_session_ids,
    _filtered_sorted_sessions,
    _index_mtime,
    _index_session_meta,
    _normalize_group_mode,
    _peek_cache_bucket,
    _redirect_after_mutation,
    _reject_summary_provider_params,
    _session_peek_html_cached,
    _sessions_day_groups,
    _sessions_list_back_path,
    _tombstone_session,
    _unlink_session_export,
    _user_tags_by_session,
    _validated_bulk_session_ids,
    api_session_find,
    api_session_resume,
    api_session_summary,
    api_stats_costs,
    dashboard,
    export_index,
    export_session,
    load_session_by_id,
    projects,
    session_delete,
    session_detail,
    session_message_permalink,
    session_messages_fragment,
    session_peek,
    session_summary_generate,
    sessions,
    sessions_bulk_delete,
    sessions_list_now,
    sessions_query_url,
    sessions_rows,
    stats_page,
    thread_detail,
    threads,
)
from .routes.tags import (  # noqa: E402,F401  (re-exports)
    api_all_tags,
    api_session_tags,
    sessions_bulk_tag,
)


def create_app() -> Flask:
    """Return the process-wide Flask app with every route blueprint registered."""
    register_blueprints(app)
    return app


create_app()
