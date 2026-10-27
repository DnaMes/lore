"""HTML search endpoint (#119)."""

from flask import Blueprint, jsonify, request

from lore.interfaces import web as _web
from lore.utils.security import (
    validate_search_param,
    validate_search_query,
    validate_tool_name,
)
from lore.utils.tooling import normalize_tool_name

from ..api_payloads import search_result_snippet, serialize_search_evidence

bp = Blueprint("search", __name__)


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
                "date": r["session"].get("created")[:10],
                "snippet": search_result_snippet(r),
                "evidence": serialize_search_evidence(r),
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
