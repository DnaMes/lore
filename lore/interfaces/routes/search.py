"""HTML and JSON search endpoints (#119)."""

from urllib.parse import urlencode

from flask import Blueprint, jsonify, request

from lore.interfaces import web as _web
from lore.services.index import count_index_hits
from lore.utils.security import (
    validate_search_param,
    validate_search_query,
    validate_tool_name,
)
from lore.utils.tooling import normalize_tool_name

from ..api_payloads import (
    search_result_snippet,
    serialize_search_evidence,
    serialize_search_result,
)
from ..web_helpers import get_style, parse_date_param

bp = Blueprint("search", __name__)

SEARCH_PER_PAGE = 20


def _search_page_url(
    q: str,
    tool: str,
    project: str,
    start: str,
    end: str,
    tag: str,
    page: int,
    include_automated: bool,
) -> str:
    params = {}
    if q:
        params["q"] = q
    if tool:
        params["tool"] = tool
    if project:
        params["project"] = project
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if tag:
        params["tag"] = tag
    if include_automated:
        params["automated"] = "1"
    if page > 1:
        params["page"] = str(page)
    query_string = urlencode(params)
    return f"/search?{query_string}" if query_string else "/search"


@bp.route("/search")
def search_page():
    q = (request.args.get("q") or "").strip()
    tool_raw = (request.args.get("tool") or "").strip()
    project = (request.args.get("project") or "").strip()
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    page_raw = (request.args.get("page") or "1").strip()

    idx = _web._visible_index()
    tools = sorted((idx.get("stats", {}).get("by_tool") or {}).keys())

    error = None
    tool = ""
    filters: dict = {}
    page = 1

    if q and not validate_search_query(q):
        error = "Invalid search query."
    elif tool_raw and not validate_tool_name(tool_raw):
        error = "Invalid tool parameter."
    else:
        tool = normalize_tool_name(tool_raw) or ""
        if project and not validate_search_param(project):
            error = "Invalid project parameter."

    if not error:
        try:
            page = int(page_raw)
        except ValueError:
            error = "Invalid page parameter."
        else:
            if page < 1:
                error = "Invalid page parameter."

    if not error:
        try:
            filters = _web._search_filter_params()
        except ValueError as exc:
            error = str(exc)

    if not error:
        for name in ("start", "end"):
            value = filters.get(name)
            if value and parse_date_param(value, end=(name == "end")) is None:
                error = f"Invalid {name} date. Use YYYY-MM-DD."
                break

    title = f"Search: {q}" if q else "Search"
    if error:
        return (
            _web.render(
                "search",
                active="",
                title=title,
                q=q,
                tool=tool or tool_raw,
                project=project,
                start=start,
                end=end,
                tools=tools,
                results=[],
                total=0,
                page=1,
                per_page=SEARCH_PER_PAGE,
                prev_url="",
                next_url="",
                error=error,
            ),
            400,
        )

    results = []
    total = None
    if q:
        offset = (page - 1) * SEARCH_PER_PAGE
        try:
            raw_results = _web.search_index(
                q,
                tool=tool or None,
                project=project or None,
                limit=SEARCH_PER_PAGE,
                offset=offset,
                **_web._automated_kwargs(),
                **filters,
            )
            total = count_index_hits(
                _web.INDEX_PATH,
                q,
                _web.load_deleted_session_ids(_web.DELETED_SESSIONS_PATH),
                tool=tool or None,
                project=project or None,
                include_automated=_web._automated_kwargs().get("include_automated", False),
                **filters,
            )
        except ValueError as exc:
            return (
                _web.render(
                    "search",
                    active="",
                    title=title,
                    q=q,
                    tool=tool,
                    project=project,
                    start=start,
                    end=end,
                    tools=tools,
                    results=[],
                    total=0,
                    page=1,
                    per_page=SEARCH_PER_PAGE,
                    prev_url="",
                    next_url="",
                    error=str(exc),
                ),
                400,
            )
        results = [serialize_search_result(r) for r in raw_results]

    include_automated = bool(_web._automated_kwargs().get("include_automated", False))
    tag = filters.get("tag", "")
    prev_url = (
        _search_page_url(q, tool, project, start, end, tag, page - 1, include_automated)
        if q and page > 1
        else ""
    )
    has_next = bool(
        q
        and (
            (page * SEARCH_PER_PAGE < total)
            if total is not None
            else (len(results) == SEARCH_PER_PAGE)
        )
    )
    next_url = (
        _search_page_url(q, tool, project, start, end, tag, page + 1, include_automated)
        if has_next
        else ""
    )

    return _web.render(
        "search",
        active="",
        title=title,
        q=q,
        tool=tool,
        project=project,
        start=start,
        end=end,
        tools=tools,
        results=results,
        total=total,
        page=page,
        per_page=SEARCH_PER_PAGE,
        prev_url=prev_url,
        next_url=next_url,
        error=None,
    )


@bp.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if len(q) < 2:
        return jsonify([])

    if not validate_search_query(q):
        return jsonify({"error": "Invalid search query"}), 400

    tool = request.args.get("tool", "")
    if tool and not validate_tool_name(tool):
        return jsonify({"error": "Invalid tool parameter"}), 400
    tool = normalize_tool_name(tool) or ""

    project = request.args.get("project", "")
    if project and not validate_search_param(project):
        return jsonify({"error": "Invalid project parameter"}), 400

    try:
        filters = _web._search_filter_params()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        res = _web.search_index(
            q,
            tool=tool or None,
            project=project or None,
            limit=10,
            **_web._automated_kwargs(),
            **filters,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        [
            {
                "id": r["session"].get("id"),
                "title": r["session"].get("title"),
                "tool": r["session"].get("tool"),
                "tool_name": get_style(r["session"].get("tool")).get("name")
                or r["session"].get("tool"),
                "date": (r["session"].get("created") or "")[:10],
                "snippet": search_result_snippet(r),
                "evidence": serialize_search_evidence(r),
                "match": r.get("match") or "keyword",
                "source": "session",
            }
            for r in res
        ]
    )


def _search_filter_params() -> dict:
    """Validated ``tag``/``start``/``end`` search filters (#174).

    Empty values are omitted so the search keeps its defaults. Invalid dates are
    reported as a 400 by the caller instead of silently widening the result set.
    """
    filters: dict = {}
    tag = (request.args.get("tag") or "").strip()
    if tag:
        if not validate_search_param(tag):
            raise ValueError("Invalid tag parameter")
        filters["tag"] = tag
    for name in ("start", "end"):
        value = (request.args.get(name) or "").strip()
        if value:
            filters[name] = value
    return filters
