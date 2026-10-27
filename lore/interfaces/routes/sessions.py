"""Session reading, listing, export and page routes (#119)."""

import html
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import nh3
from flask import Blueprint, Response, g, has_request_context, jsonify, redirect, request

from lore.interfaces import web as _web
from lore.utils.security import (
    validate_search_param,
    validate_session_id,
    validate_tool_name,
)
from lore.utils.text_processing import format_thinking
from lore.utils.tooling import normalize_tool_name

from ...extractors.opencode import OpenCodeExtractor
from ...services.cache import threadsafe_lru_cache
from ...services.index import is_automated, session_by_id_map
from ...services.summarize_cli import web_summarize_enabled
from ..web_data import (
    _sanitize_next_url,
    build_session_from_export_markdown,
    clear_index_cache,
    load_export_lookup,
    remember_deleted_session_id,
)
from ..web_formatting import (
    SANITIZE_ATTRS,
    SANITIZE_TAGS,
    SANITIZE_URL_SCHEMES,
    format_message_content,
    format_tool_calls,
    group_sessions_for_list,
    render_markdown,
    sanitize_rendered_html,
    session_date_range_chips,
    session_day_iso,
)
from ..web_helpers import TOOL_STYLES, get_style, parse_date_param, project_label
from ..web_jobs import _start_session_summary_job
from ..web_peek import build_peek_context
from ..web_services import (
    build_projects_payload,
    build_thread_detail_payload,
    build_threads_overview,
    compute_top_tags,
    enrich_session_for_detail,
)
from ..web_summary import (
    api_session_summary_payload,
    build_summary_card_context,
    session_summary_lines,
)

bp = Blueprint("sessions", __name__)

logger = logging.getLogger(__name__)


@bp.route("/")
def dashboard():
    idx = _web._visible_index()
    recent = sorted(
        idx.get("sessions", []),
        key=lambda s: s.get("updated") or s.get("created") or "",
        reverse=True,
    )[:10]
    return _web.render(
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


def sessions_list_now() -> datetime:
    """Injectable clock for session list day labels (tests)."""
    return datetime.now()


def _normalize_group_mode(value: str) -> str:
    return "flat" if value == "flat" else "day"


def sessions_query_url(**updates: str) -> str:
    """Build a /sessions URL preserving current query params."""
    if not has_request_context():
        return "/sessions"
    merged = {k: v for k, v in request.args.items() if k not in ("page", "after_day")}
    for key, value in updates.items():
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)
    if not merged:
        return "/sessions"
    return "/sessions?" + urlencode(merged)


def _sessions_list_back_path() -> str:
    if not has_request_context():
        return "/sessions"
    return sessions_query_url()


def _sessions_day_groups(
    page_sessions: list,
    group_mode: str,
    after_day: str,
) -> tuple[list[dict[str, Any]], str]:
    now = _web.sessions_list_now()
    effective_after = after_day if group_mode == "day" else ""
    day_groups = group_sessions_for_list(
        page_sessions,
        group_mode=group_mode,
        after_day=effective_after or None,
        now=now,
    )
    last_day = session_day_iso(page_sessions[-1]) if page_sessions else ""
    return day_groups, last_day or ""


def _index_mtime():
    """Return the current index mtime used to invalidate session-list caches."""
    try:
        return _web.INDEX_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _user_tags_by_session():
    """Saved tags of the whole index, keyed by session id (#173).

    The tag filter has to see the user's own tags, which live in the v2
    ``session_tags`` table, not in the flat index keywords. The read is one
    query and only happens when a tag filter is active, so it is deliberately
    not cached: a tag edit must take effect on the next request.
    """
    from ...storage import all_tags_by_session

    return all_tags_by_session(_web.OUTPUT_DIR)


@threadsafe_lru_cache(maxsize=128)
def _filtered_sorted_session_ids(tool, tag, start, end, index_mtime, include_automated=False):
    """Apply list filters and sort newest-first.

    Returns ``(session ids, automated sessions left out)``. The count is taken after the
    tool/date filters, so a scoped view reports its own number. A tag filter skips the
    origin filter: a tagged automated session stays visible.
    """
    all_s = _web.load_index().get("sessions", [])
    start_dt = parse_date_param(start)
    end_dt = parse_date_param(end, end=True)
    user_tags = _user_tags_by_session() if tag else None
    filtered = _web.filter_sessions(
        all_s,
        tool=tool or None,
        tag=tag or None,
        start=start_dt,
        end=end_dt,
        user_tags=user_tags,
    )
    hidden = 0
    if not include_automated and not tag:
        visible = [s for s in filtered if not is_automated(s)]
        hidden = len(filtered) - len(visible)
        filtered = visible
    filtered = sorted(
        filtered,
        key=lambda s: s.get("updated") or s.get("created") or "",
        reverse=True,
    )
    return tuple(session.get("id") for session in filtered), hidden


def _filtered_sorted_sessions(tool, tag, start, end):
    """Apply list filters and sort newest-first. Returns the full filtered list."""
    session_ids, _hidden = _filtered_sorted_session_ids(
        tool, tag, start, end, _index_mtime(), _web._include_automated()
    )
    all_s = _web.load_index().get("sessions", [])
    sessions_by_id = {session.get("id"): session for session in all_s}
    return [
        sessions_by_id[session_id] for session_id in session_ids if session_id in sessions_by_id
    ]


def _filtered_automated_hidden(tool, tag, start, end) -> int:
    """Automated sessions the same filters leave out (same cache entry, no extra pass)."""
    return _filtered_sorted_session_ids(
        tool, tag, start, end, _index_mtime(), _web._include_automated()
    )[1]


@bp.route("/sessions")
def sessions():
    tool = request.args.get("tool", "")
    tag = request.args.get("tag", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    group = _normalize_group_mode(request.args.get("group", "day"))

    if tool and not validate_tool_name(tool):
        return "Invalid tool parameter", 400
    tool = normalize_tool_name(tool) or ""

    if tag and not validate_search_param(tag):
        return "Invalid tag parameter", 400

    filtered = _filtered_sorted_sessions(tool, tag, start, end)
    total = len(filtered)
    pages = max(1, math.ceil(total / _web.SESSIONS_PER_PAGE))
    first_page = filtered[: _web.SESSIONS_PER_PAGE]
    day_groups, last_day = _sessions_day_groups(first_page, group, "")
    summary_lines = session_summary_lines(
        _web.OUTPUT_DIR,
        [s.get("id") for s in first_page],
        {str(s.get("id")): s.get("updated") or s.get("created") for s in first_page},
    )

    from ...storage import list_all_tags

    tags = compute_top_tags(
        _web._visible_index().get("sessions", []),
        limit=12,
        user_tag_counts=list_all_tags(_web.OUTPUT_DIR),
    )
    return _web.render(
        "sessions",
        automated_hidden=_filtered_automated_hidden(tool, tag, start, end),
        active="sessions",
        sessions=first_page,
        day_groups=day_groups,
        last_day=last_day,
        group=group,
        date_chips=session_date_range_chips(
            now=_web.sessions_list_now(), active_start=start, active_end=end
        ),
        total=total,
        pages=pages,
        per_page=_web.SESSIONS_PER_PAGE,
        tools=list(TOOL_STYLES.keys()),
        tool=tool,
        tag=tag,
        tags=tags,
        start=start,
        end=end,
        back_to=(request.full_path[:-1] if request.full_path.endswith("?") else request.full_path),
        summary_lines=summary_lines,
        title="Sessions",
    )


@bp.route("/sessions/rows")
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

    group = _normalize_group_mode(request.args.get("group", "day"))
    after_day = request.args.get("after_day", "").strip()

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return "Invalid page parameter", 400
    if page < 1:
        return "Invalid page parameter", 400

    filtered = _filtered_sorted_sessions(tool, tag, start, end)
    offset = (page - 1) * _web.SESSIONS_PER_PAGE
    page_sessions = filtered[offset : offset + _web.SESSIONS_PER_PAGE]
    day_groups, last_day = _sessions_day_groups(page_sessions, group, after_day)
    summary_lines = session_summary_lines(
        _web.OUTPUT_DIR,
        [s.get("id") for s in page_sessions],
        {str(s.get("id")): s.get("updated") or s.get("created") for s in page_sessions},
    )

    html = _web.render(
        "session_rows",
        sessions=page_sessions,
        day_groups=day_groups,
        last_day=last_day,
        back_to=_sessions_list_back_path(),
        summary_lines=summary_lines,
    )
    return Response(html, mimetype="text/html")


def _peek_cache_bucket(session_id: str) -> str:
    meta = _web._index_session_meta(session_id) or {}
    from lore.storage.summaries import get_summary

    row = get_summary(_web.OUTPUT_DIR, "session", f"session:{session_id}")
    summary_stamp = str((row or {}).get("created") or "")
    return ":".join(
        [
            str(_index_mtime()),
            str(meta.get("updated") or meta.get("created") or ""),
            str(meta.get("display_title") or meta.get("title") or ""),
            summary_stamp,
        ]
    )


@threadsafe_lru_cache(maxsize=128)
def _session_peek_html_cached(session_id: str, _index_bucket: str) -> str:
    meta = _web._index_session_meta(session_id)
    if not meta:
        return ""
    resolved = _web._enriched_session_for_detail(session_id, force_live=False)
    session_obj = resolved[0] if resolved else None
    ctx = build_peek_context(_web.OUTPUT_DIR, session_id, meta, session_obj)
    return _web.render("session_peek", **ctx)


@bp.route("/sessions/<session_id>/peek")
def session_peek(session_id):
    """Read-only HTML preview fragment for the sessions list peek pane."""
    if not validate_session_id(session_id):
        return "Invalid session ID", 400
    deleted = _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH)
    if session_id in deleted:
        return "Not found", 404
    if not _web._index_session_meta(session_id):
        return "Not found", 404
    html_fragment = _session_peek_html_cached(session_id, _peek_cache_bucket(session_id))
    if not html_fragment:
        return "Not found", 404
    g.peek_cache_max_age = 30
    return Response(html_fragment, mimetype="text/html")


@bp.route("/projects")
def projects():
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
    end_dt = parse_date_param(end, end=True)
    filtered = _web.filter_sessions(
        _web.load_index().get("sessions", []),
        tool=tool or None,
        tag=tag or None,
        start=start_dt,
        end=end_dt,
    )
    hidden: list = []
    filtered = _web._origin_scoped(filtered, tag, hidden)

    projects = build_projects_payload(filtered, project_label)
    from ...storage import list_all_tags

    tags = compute_top_tags(
        _web._visible_index().get("sessions", []),
        limit=12,
        user_tag_counts=list_all_tags(_web.OUTPUT_DIR),
    )
    return _web.render(
        "projects",
        automated_hidden=hidden[0] if hidden else 0,
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

    # Money is spent regardless of origin: costs always cover every session.
    sessions = _web.load_index().get("sessions", [])

    total_tokens = 0
    session_count = 0
    untokened_count = 0
    automated_included = 0
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
        if is_automated(session):
            automated_included += 1

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
        "automated_included": automated_included,
        "automated_included_line": (
            f"includes {automated_included} automated session"
            f"{'' if automated_included == 1 else 's'}"
            if automated_included
            else ""
        ),
    }


@bp.route("/api/stats/costs")
def api_stats_costs():
    return jsonify(_build_costs_payload())


@bp.route("/stats")
def stats_page():
    """Activity statistics over the last 30 days.

    The flat index carries no token/cost data, so this reports real
    activity — sessions per day/tool/project, message and prompt counts —
    using the same aggregation as the `lore digest` command.
    """
    from datetime import timedelta

    from lore.digest import build_digest

    sessions = _web._visible_index().get("sessions", [])
    since = datetime.now() - timedelta(days=30)
    digest = build_digest(sessions, since=since, top_n_projects=10)

    # Continuous 30-day series (fill gaps with 0) for the bar chart.
    by_day_series = []
    for i in range(30):
        day = (datetime.now() - timedelta(days=29 - i)).strftime("%Y-%m-%d")
        by_day_series.append({"date": day, "count": digest.by_day.get(day, 0)})
    peak_day = max((d["count"] for d in by_day_series), default=0)

    return _web.render(
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
            direct = _web.find_live_session(session_id, tool or None)
        except Exception as exc:
            logger.debug("Direct session lookup failed for %s: %s", tool, exc)
            direct = None
        if direct is not None:
            return direct
        try:
            sessions = _web.load_sessions_for_tool(tool if tool else None)
        except Exception as exc:
            logger.debug("Failed loading sessions for tool %s: %s", tool, exc)
            continue
        for session in sessions:
            if session.session_id == session_id:
                return session
    return None


def _index_session_meta(session_id: str) -> Optional[dict[str, Any]]:
    # O(1) dict hit via the payload-identity-keyed memo in the service
    # layer (#125); _web.load_index() stays in the path so test fakes of
    # web.load_index keep driving this lookup.
    return session_by_id_map(_web.load_index()).get(session_id)


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
    meta = _web._index_session_meta(session_id)
    if meta:
        export_path = _web.resolve_export_path(meta.get("export_path"))
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
        from ...storage import load_session_messages_v2

        v2_messages = load_session_messages_v2(_web.OUTPUT_DIR, session_id)
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
    session_meta = _web._index_session_meta(session_id)
    if not session_meta:
        return None

    tool_filter = normalize_tool_name(session_meta.get("tool") or "") or session_meta.get("tool")
    export_path = _web.resolve_export_path(session_meta.get("export_path"))
    if not export_path and _web._export_fallback_scan_enabled():
        export_path = load_export_lookup().get(session_id)
    noise_rules = _web.load_noise_rules()

    def _enrich(session_obj):
        # Backfill per-message tokens/model from the v2 store onto the served
        # session, so chips _web.render without ?live=1 (#62). The export-_web.markdown
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
            return _web.load_session_by_id(
                session_id,
                preferred_tool=tool_filter,
                allow_cross_tool_fallback=allow_cross_tool_fallback,
            )
        if allow_cross_tool_fallback:
            return _web.load_session_by_id(session_id, allow_cross_tool_fallback=True)
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
        # No parsable session — caller falls back to raw _web.markdown _web.render.
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
        live_session = _web.load_session_by_id(
            session_id, preferred_tool=tool_filter, allow_cross_tool_fallback=False
        )
        if not live_session and force_live:
            live_session = _web.load_session_by_id(
                session_id, preferred_tool=tool_filter, allow_cross_tool_fallback=True
            )
    elif force_live:
        live_session = _web.load_session_by_id(session_id, allow_cross_tool_fallback=True)

    if live_session:
        return _enrich(live_session)
    return None


@bp.route("/session/<session_id>/messages")
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

    if not _web._index_session_meta(session_id):
        return "Not found", 404

    force_live = (request.args.get("live") or "").strip() == "1"
    resolved = _web._enriched_session_for_detail(session_id, force_live=force_live)
    if not resolved:
        return "Not found", 404

    session_obj, _toc = resolved
    pairs = getattr(session_obj, "pairs", []) or []
    offset = (page - 1) * MESSAGES_PER_PAGE
    page_pairs = pairs[offset : offset + MESSAGES_PER_PAGE]

    html_fragment = _web.render(
        "session_pairs",
        pairs=page_pairs,
        pair_offset=offset,
        style=get_style(session_obj.tool.value),
    )
    return Response(html_fragment, mimetype="text/html")


@bp.route("/session/<session_id>")
def session_detail(session_id):
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    idx = _web.load_index()
    session_meta = next((s for s in idx.get("sessions", []) if s.get("id") == session_id), None)

    if not session_meta:
        return "Not found", 404

    # Use the index-computed display_title (which already drops low-quality
    # titles like the <local-command-caveat> leak) for the page-title / headline,
    # never the raw, possibly-noisy session.title (#68). A good live title is
    # preferred over this below, when the live extractor provides one.
    clean_title = session_meta.get("display_title") or session_meta.get("title") or "Session"

    back_target = _sanitize_next_url(request.args.get("back", "")) or "/sessions"
    export_path = _web.resolve_export_path(session_meta.get("export_path"))
    if not export_path and _web._export_fallback_scan_enabled():
        export_path = load_export_lookup().get(session_id)

    def _render_markdown(path_value):
        md_text = Path(path_value).read_text(encoding="utf-8")
        if _web.markdown:
            html_content = render_markdown(md_text)
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

        return _web.render(
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
        return _web.render(
            "rules",
            active="sessions",
            rules=summary,
            title=clean_title,
        )

    force_live = (request.args.get("live") or "").strip() == "1"

    resolved = _web._enriched_session_for_detail(session_id, force_live=force_live)
    if resolved:
        session_obj, toc = resolved
        # The <h1> headline reads session.title directly. Only replace it with the
        # cleaned title when the object's own title is missing or low-quality (e.g.
        # the <local-command-caveat> leak) — a good live-extractor title wins (#68).
        from ...exporters.index import is_low_quality_title

        obj_title = (session_obj.title or "").strip()
        if not obj_title or is_low_quality_title(obj_title):
            session_obj.title = clean_title
        pairs = getattr(session_obj, "pairs", []) or []
        total_pairs = len(pairs)
        message_pages = max(1, math.ceil(total_pairs / MESSAGES_PER_PAGE))
        visible_pairs = pairs[:MESSAGES_PER_PAGE]
        deleted_ids = _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH)
        summary_ctx = build_summary_card_context(
            _web.OUTPUT_DIR,
            session_id,
            session_obj,
            visible_pairs,
            session_deleted=session_id in deleted_ids,
        )
        return _web.render(
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
            **summary_ctx,
        )

    # No paginatable conversation — fall back to raw _web.markdown or a summary card.
    if export_path and not force_live:
        try:
            return _render_markdown(export_path)
        except OSError as exc:
            logger.debug(
                "Failed to _web.render export _web.markdown for session %s: %s", session_id, exc
            )

    return _render_summary()


_SUMMARY_FORBIDDEN_PARAMS = frozenset({"provider", "model", "base_url", "base-url"})


def _reject_summary_provider_params() -> Optional[tuple]:
    """Web summary routes never accept provider/model/base-url overrides."""
    for key in _SUMMARY_FORBIDDEN_PARAMS:
        if (request.args.get(key) or "").strip():
            return jsonify({"error": f"Parameter '{key}' is not allowed"}), 400
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for key in _SUMMARY_FORBIDDEN_PARAMS:
            if key in payload and payload.get(key) not in (None, ""):
                return jsonify({"error": f"Parameter '{key}' is not allowed"}), 400
    return None


@bp.route("/api/session/<session_id>/find")
def api_session_find(session_id):
    from ..session_find import find_session_matches, validate_find_query

    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    q = (request.args.get("q") or "").strip()
    err = validate_find_query(q)
    if err:
        return jsonify({"error": err}), 400
    deleted = _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH)
    if session_id in deleted:
        return jsonify({"error": "Not found"}), 404
    if not _web._index_session_meta(session_id):
        return jsonify({"error": "Not found"}), 404
    include_reasoning = (request.args.get("include_reasoning") or "0").strip().lower() in (
        "1",
        "true",
    )
    force_live = (request.args.get("live") or "").strip() == "1"
    resolved = _web._enriched_session_for_detail(session_id, force_live=force_live)
    if not resolved:
        return jsonify({"error": "Not found"}), 404
    session_obj, _toc = resolved
    return jsonify(
        find_session_matches(
            session_obj,
            q,
            include_reasoning=include_reasoning,
            per_page=MESSAGES_PER_PAGE,
        )
    )


@bp.route("/api/session/<session_id>/summary")
def api_session_summary(session_id):
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    deleted = _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH)
    if session_id in deleted:
        return jsonify({"error": "Not found"}), 404
    if not _web._index_session_meta(session_id):
        return jsonify({"error": "Not found"}), 404
    body = api_session_summary_payload(_web.OUTPUT_DIR, session_id)
    if body is None:
        return jsonify(
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
    return jsonify(body)


@bp.route("/session/<session_id>/summary", methods=["POST"])
def session_summary_generate(session_id):
    if (err := _web._check_local_origin()) is not None:
        return err
    if (param_err := _reject_summary_provider_params()) is not None:
        return param_err
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    deleted = _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH)
    if session_id in deleted:
        return jsonify({"error": "Session is deleted"}), 404
    if not _web._index_session_meta(session_id):
        return jsonify({"error": "Not found"}), 404
    if not web_summarize_enabled():
        return jsonify({"error": "Local summary provider is not configured"}), 403
    job_id = _start_session_summary_job(session_id)
    return (
        jsonify({"status": "accepted", "job_id": job_id, "session_id": session_id}),
        202,
    )


@bp.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_session_resume(session_id):
    if (err := _web._check_local_origin()) is not None:
        return err
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _web._index_session_meta(session_id)
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


BULK_SESSION_ACTION_LIMIT = 200


def _unlink_session_export(session_meta: dict) -> None:
    export_path_str = session_meta.get("export_path")
    if not export_path_str:
        return
    export_path = _web.resolve_export_path(export_path_str)
    if export_path and export_path.exists():
        try:
            export_path.unlink()
        except OSError:
            pass


def _tombstone_session(session_id: str) -> None:
    remember_deleted_session_id(session_id)
    clear_index_cache()


def _redirect_after_mutation():
    next_url = _sanitize_next_url(request.form.get("next", ""))
    if not next_url:
        referer = request.headers.get("Referer", "")
        parsed = urlparse(referer)
        if not parsed.netloc or parsed.netloc == request.host:
            candidate = (parsed.path or "") + (("?" + parsed.query) if parsed.query else "")
            next_url = _sanitize_next_url(candidate)
    return redirect(next_url or "/sessions")


def _validated_bulk_session_ids() -> tuple[Optional[list[str]], Optional[tuple[str, int]]]:
    session_ids = request.form.getlist("session_id")
    if not session_ids:
        return None, ("No sessions selected", 400)
    if len(session_ids) > BULK_SESSION_ACTION_LIMIT:
        return None, ("Too many sessions", 400)
    for session_id in session_ids:
        if not validate_session_id(session_id):
            return None, ("Invalid session ID", 400)
    indexed = {str(s.get("id")) for s in _web.load_index().get("sessions", [])}
    for session_id in session_ids:
        if session_id not in indexed:
            return None, ("Invalid session ID", 400)
    return session_ids, None


@bp.route("/session/<session_id>/delete", methods=["POST"])
def session_delete(session_id):
    if (err := _web._check_local_origin()) is not None:
        return err
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    # Find the session through _web.load_index() so this works regardless of
    # whether the active store is v2 or the legacy JSON index (#44).
    sessions = _web.load_index().get("sessions", [])
    session_meta = next((s for s in sessions if s.get("id") == session_id), None)

    if not session_meta:
        return "Not found", 404

    _unlink_session_export(session_meta)
    _tombstone_session(session_id)
    return _redirect_after_mutation()


@bp.route("/sessions/bulk-delete", methods=["POST"])
def sessions_bulk_delete():
    if (err := _web._check_local_origin()) is not None:
        return err
    session_ids, err_body = _validated_bulk_session_ids()
    if err_body is not None:
        return err_body

    sessions = _web.load_index().get("sessions", [])
    meta_by_id = {str(s.get("id")): s for s in sessions}
    for session_id in session_ids or []:
        _unlink_session_export(meta_by_id[session_id])
        _tombstone_session(session_id)  # the same path as the single delete route
    return _redirect_after_mutation()


@bp.route("/threads")
def threads():
    idx = _web._visible_index()
    threads_sorted = build_threads_overview(idx.get("sessions", []))
    return _web.render("threads", active="threads", threads=threads_sorted, title="Threads")


@bp.route("/thread/<thread_id>")
def thread_detail(thread_id):
    if not validate_session_id(thread_id):
        return "Invalid thread id", 400

    payload = build_thread_detail_payload(
        thread_id,
        _web._thread_sessions(thread_id),
        _web.load_sessions_for_tool,
        format_message_content,
        format_tool_calls,
        get_style,
        noise_rules=_web.load_noise_rules(),
        sanitize_rendered_html_fn=sanitize_rendered_html,
    )
    if not payload["thread_timeline"]:
        return "Not found", 404

    return _web.render(
        "thread_detail",
        active="threads",
        thread_id=thread_id,
        messages=payload["messages"],
        toc_items=payload["toc_items"],
        continue_cmd=payload["continue_cmd"],
        thread_meta=payload["thread_meta"],
        thread_groups=payload["thread_groups"],
        thread_timeline=payload["thread_timeline"],
        automated_hidden=_web._thread_hidden(thread_id),
        title="Thread",
    )


@bp.route("/session/<session_id>/m/<int:seq>")
def session_message_permalink(session_id, seq):
    """Deep link to one message with bounded context (#171).

    Renders only the requested message and its neighbours instead of the whole
    transcript, which is what makes a permalink cheap on a 20 000-message
    session. Unknown sessions return 404; a message id the store does not know
    (deleted, or a session whose rows were never synced) returns 410 with the
    reason, never an empty page.
    """
    if not validate_session_id(session_id):
        return "Invalid session ID", 400
    session_meta = _web._index_session_meta(session_id)
    if not session_meta:
        return _web.render("message_permalink_missing", session_id=session_id, seq=seq), 404

    window = _web._bounded_message_window(session_id, offset=0, limit=21, around_seq=seq)
    if window["source"] == "v2" and window["found_seq"] is None:
        return _web.render("message_permalink_missing", session_id=session_id, seq=seq), 410

    target = next((message for message in window["messages"] if message["seq"] == seq), None)
    if target is None:
        return _web.render("message_permalink_missing", session_id=session_id, seq=seq), 404

    style = get_style(session_meta.get("tool") or "")
    return _web.render(
        "message_permalink",
        active="sessions",
        session_id=session_id,
        session_title=session_meta.get("display_title") or session_meta.get("title") or "",
        seq=seq,
        total=window["total"],
        messages=[
            {
                **message,
                "formatted_content": format_message_content(message["content"]),
            }
            for message in window["messages"]
        ],
        style=style,
        source=window["source"],
    )


@bp.route("/export/index")
def export_index():
    if not _web.INDEX_PATH.exists():
        return "Index not found", 404
    with open(_web.INDEX_PATH, "r", encoding="utf-8") as f:
        data = f.read()
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="index.json"'},
    )


@bp.route("/export/<session_id>")
def export_session(session_id):
    if not validate_session_id(session_id):
        return "Invalid session ID", 400

    want_redact = request.args.get("redact") == "1"

    idx = _web.load_index()
    session_meta = next((s for s in idx.get("sessions", []) if s.get("id") == session_id), None)
    from ...exporters.markdown import MarkdownExporter
    from ...exporters.redaction import redact_session
    from ...utils.redact import redact

    def _markdown_response(body: str, redactions: int = 0) -> Response:
        headers = {
            "Content-Disposition": f'attachment; filename="{session_id}.md"',
        }
        if want_redact:
            headers["X-Lore-Redactions"] = str(redactions)
        return Response(body, mimetype="text/_web.markdown", headers=headers)

    if session_meta:
        export_path = _web.resolve_export_path(session_meta.get("export_path"))
        if export_path and export_path.exists():
            body = export_path.read_text(encoding="utf-8", errors="replace")
            if want_redact:
                result = redact(body)
                return _markdown_response(result.text, result.count)
            return Response(
                body,
                mimetype="text/_web.markdown",
                headers={"Content-Disposition": f'attachment; filename="{export_path.name}"'},
            )

        tool_filter = normalize_tool_name(session_meta.get("tool") or "")
        if tool_filter:
            session = _web.load_session_by_id(session_id, preferred_tool=tool_filter)
            if session:
                if want_redact:
                    session, redactions = redact_session(session)
                else:
                    redactions = 0
                return _markdown_response(
                    MarkdownExporter(_web.OUTPUT_DIR)._generate_markdown(session),
                    redactions,
                )

    if os.environ.get("LORE_EXPORT_FALLBACK_SCAN", "").lower() != "true":
        return "Not found", 404

    for ex in _web.get_all_extractors():
        if not ex.is_available():
            continue
        for session in ex.extract_sessions():
            if session.session_id == session_id:
                if want_redact:
                    session, redactions = redact_session(session)
                else:
                    redactions = 0
                return _markdown_response(
                    MarkdownExporter(_web.OUTPUT_DIR)._generate_markdown(session),
                    redactions,
                )
    return "Not found", 404
