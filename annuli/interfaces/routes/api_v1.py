"""``/api/v1`` — the versioned, read-only JSON API (#119).

Every helper that the test suite patches on ``lore.interfaces.web`` is reached
through that module (``_web.<name>``) at call time, never bound at import time;
see "Web module layout" in ``docs/WEB-UI.md``.
"""

import math

from flask import Blueprint, jsonify, request

from lore.interfaces import web as _web
from lore.utils.security import (
    validate_search_param,
    validate_search_query,
    validate_session_id,
    validate_tool_name,
)
from lore.utils.tooling import normalize_tool_name

from ...services.index import automated_hidden_line, count_index_hits
from ...storage import load_session_message_window
from ..api_payloads import (
    message_permalink,
    serialize_index_session_summary,
    serialize_live_session,
    serialize_project,
    serialize_search_result,
    serialize_thread_messages,
    serialize_thread_overview,
)
from ..web_formatting import format_message_content, format_tool_calls, sanitize_rendered_html
from ..web_helpers import get_style, project_label
from ..web_services import (
    build_projects_payload,
    build_thread_detail_payload,
    build_threads_overview,
)

bp = Blueprint("api_v1", __name__)


@bp.route("/api/v1/search")
def api_v1_search():
    q = request.args.get("q", "")
    if len(q) < 2:
        return jsonify({"query": q, "count": 0, "results": []})

    if not validate_search_query(q):
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
        # `per_page` is the documented name; `limit` stays accepted for older
        # clients. `page` is 1-based, so every match is reachable (#175).
        per_page = _api_limit_param(name="per_page", default=20, max_value=200)
        if not request.args.get("per_page") and request.args.get("limit"):
            per_page = _api_limit_param(default=20, max_value=200)
        page = int(request.args.get("page", "1"))
        filters = _web._search_filter_params()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if page < 1:
        return jsonify({"error": "Invalid page parameter"}), 400

    # `offset` is only passed when it is non-zero, so callers that wrap
    # `search_index` with the older signature keep working for the first page.
    paging = {"offset": (page - 1) * per_page} if page > 1 else {}
    try:
        results = _web.search_index(
            q,
            tool=tool or None,
            project=project or None,
            limit=per_page,
            scope=scope,
            **_web._automated_kwargs(),
            **filters,
            **paging,
        )
        total = count_index_hits(
            _web.INDEX_PATH,
            q,
            _web.load_deleted_session_ids(_web.OUTPUT_DIR / "deleted_sessions.json"),
            tool=tool or None,
            project=project or None,
            include_automated=_web._automated_kwargs().get("include_automated", False),
            **filters,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # A role scope is applied per message after the session-level count, so the
    # total can be wider than the scoped pages; say so instead of overclaiming.
    total_is_partial = scope not in ("all", "")
    payload = {
        "query": q,
        "tool": tool or None,
        "project": project or None,
        "scope": scope,
        "tag": filters.get("tag"),
        "start": filters.get("start"),
        "end": filters.get("end"),
        "count": len(results),
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_is_partial": total_is_partial,
        "results": [serialize_search_result(result) for result in results],
    }
    if total is not None and page * per_page < total and not total_is_partial:
        payload["next_page"] = page + 1
    return jsonify(payload)


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


@bp.route("/api/v1/index/summary")
def api_v1_index_summary():
    """Return lightweight index metadata without loading full session records.

    Useful for dashboard initialisation — fetch this first (fast), then
    paginate sessions lazily via GET /api/v1/sessions?page=N&per_page=M.
    """
    summary = _web.load_index_summary(**_web._automated_kwargs())
    return jsonify(summary)


@bp.route("/api/v1/sessions")
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
    if q and not validate_search_query(q):
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
            for result in _web.search_index(
                q,
                tool=tool or None,
                project=project or None,
                limit=search_limit,
                **_web._automated_kwargs(),
            )
        ]
    else:
        all_sessions = _web._visible_index().get("sessions", [])
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


@bp.route("/api/v1/sessions/<session_id>")
def api_v1_session_detail(session_id):
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _web._index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    live = request.args.get("live", "0") == "1"
    preferred_tool = normalize_tool_name(session_meta.get("tool") or "") or None
    session_obj = _web.load_session_by_id(
        session_id,
        preferred_tool=preferred_tool,
        allow_cross_tool_fallback=live,
    )

    if session_obj:
        return jsonify(serialize_live_session(session_obj, session_meta=session_meta))

    payload = serialize_index_session_summary(session_meta)
    payload["live"] = False
    return jsonify(payload)


def _bounded_message_window(session_id: str, *, offset: int, limit: int, around_seq=None) -> dict:
    """Message window for the paging API (#171), without parsing the transcript.

    Prefers ``load_session_message_window`` (two SQL statements, bounded by
    ``LIMIT``). Falls back to the live/exports path only when the v2 store has
    no message rows for this session, which is the case for a session that was
    indexed before the row backfill.

    Returns ``{"source": "v2"|"live", "total": int, "offset": int,
    "messages": [...], "found_seq": int|None}``.
    """
    window = load_session_message_window(
        _web.OUTPUT_DIR, session_id, offset=offset, limit=limit, around_seq=around_seq
    )
    if window is not None:
        return {
            "source": "v2",
            "total": window["total"],
            "offset": window["offset"],
            "messages": [
                {**message, "permalink": message_permalink(session_id, message["seq"])}
                for message in window["messages"]
            ],
            "found_seq": window["found_seq"],
        }

    session_meta = _web._index_session_meta(session_id) or {}
    preferred_tool = normalize_tool_name(session_meta.get("tool") or "") or None
    session_obj = _web.load_session_by_id(
        session_id, preferred_tool=preferred_tool, allow_cross_tool_fallback=True
    )
    if not session_obj:
        return {"source": "live", "total": 0, "offset": 0, "messages": [], "found_seq": None}
    messages = [
        {
            "seq": index,
            "role": getattr(message.role, "value", str(message.role)),
            "content": message.content or "",
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "model": getattr(message, "model", None),
            "kind": getattr(message, "kind", None),
            "active": bool(getattr(message, "active", True)),
            "permalink": message_permalink(session_id, index),
        }
        for index, message in enumerate(session_obj.messages, start=1)
    ]
    return {
        "source": "live",
        "total": len(messages),
        "offset": 0,
        "messages": messages[offset : offset + limit],
        "found_seq": None,
    }


@bp.route("/api/v1/sessions/<session_id>/messages")
def api_v1_session_messages(session_id):
    """Bounded message window: ``?page=``/``?per_page=`` (or ``?at=<seq>``).

    Loads only the requested segment from the v2 message rows. ``total`` is the
    session's real message count, so a client can page through everything; a
    ``next_page`` is only present while more messages follow.
    """
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    session_meta = _web._index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    try:
        # ``per_page`` is the documented name; ``limit`` stays accepted for
        # clients written against the older, truncating endpoint.
        per_page = _api_limit_param(name="per_page", default=200, max_value=1000)
        if not request.args.get("per_page") and request.args.get("limit"):
            per_page = _api_limit_param(default=200, max_value=1000)
        page = int(request.args.get("page", "1"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if page < 1:
        return jsonify({"error": "Invalid page parameter"}), 400

    raw_seq = (request.args.get("at") or "").strip()
    around_seq = None
    if raw_seq:
        try:
            around_seq = int(raw_seq)
        except ValueError:
            return jsonify({"error": "Invalid at parameter"}), 400

    if around_seq is None:
        window = _bounded_message_window(session_id, offset=(page - 1) * per_page, limit=per_page)
    else:
        window = _bounded_message_window(
            session_id, offset=0, limit=per_page, around_seq=around_seq
        )
        if window["source"] == "v2" and window["found_seq"] is None:
            return jsonify({"error": "Message not found", "session_id": session_id}), 410
        page = window["offset"] // per_page + 1

    payload = serialize_index_session_summary(session_meta)
    payload.update(
        {
            "session_id": session_id,
            "message_count": len(window["messages"]),
            "total": window["total"],
            "page": page,
            "per_page": per_page,
            "source": window["source"],
            "messages": window["messages"],
        }
    )
    pages = max(1, math.ceil(window["total"] / per_page)) if window["total"] else 1
    if page < pages:
        payload["next_page"] = page + 1
    return jsonify(payload)


@bp.route("/api/v1/sessions/<session_id>/messages/<int:seq>")
def api_v1_message_permalink(session_id, seq):
    """One canonical message with a bounded neighbour window (#171)."""
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    session_meta = _web._index_session_meta(session_id)
    if not session_meta:
        return jsonify({"error": "Not found"}), 404

    window = _bounded_message_window(session_id, offset=0, limit=21, around_seq=seq)
    if window["source"] == "v2" and window["found_seq"] is None:
        return (
            jsonify(
                {
                    "error": "Message not found",
                    "reason": "deleted, or not present in the stored message rows",
                    "session_id": session_id,
                    "seq": seq,
                }
            ),
            410,
        )
    target = next((message for message in window["messages"] if message["seq"] == seq), None)
    if target is None:
        return jsonify({"error": "Message not found", "session_id": session_id}), 404
    return jsonify(
        {
            "session_id": session_id,
            "seq": seq,
            "permalink": message_permalink(session_id, seq),
            "message": target,
            "context": window["messages"],
            "total": window["total"],
            "source": window["source"],
        }
    )


@bp.route("/api/v1/projects")
def api_v1_projects():
    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    try:
        limit = _api_limit_param(default=100, max_value=500)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    sessions = _web._visible_index().get("sessions", [])
    if tool:
        sessions = [session for session in sessions if session.get("tool") == tool]
    projects = build_projects_payload(sessions, project_label)
    return jsonify(
        {
            "count": min(len(projects), limit),
            "projects": [serialize_project(project) for project in projects[:limit]],
        }
    )


@bp.route("/api/v1/threads")
def api_v1_threads():
    try:
        limit = _api_limit_param(default=100, max_value=500)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    threads = build_threads_overview(_web._visible_index().get("sessions", []))[:limit]
    return jsonify(
        {
            "count": len(threads),
            "threads": [serialize_thread_overview(thread) for thread in threads],
        }
    )


@bp.route("/api/v1/threads/<thread_id>")
def api_v1_thread_detail(thread_id):
    if not validate_session_id(thread_id):
        return jsonify({"error": "Invalid thread id"}), 400

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
        return jsonify({"error": "Not found"}), 404

    body = {
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
    hidden = _web._thread_hidden(thread_id)
    if hidden:
        body["automated_hidden"] = hidden
        body["automated_hidden_line"] = automated_hidden_line(hidden)
    return jsonify(body)
