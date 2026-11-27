"""Session tag endpoints (#119)."""

from flask import Blueprint, jsonify, request

from lore.interfaces import web as _web
from lore.utils.security import validate_search_param, validate_session_id

from ..web_data import clear_index_cache

bp = Blueprint("tags", __name__)


@bp.route("/api/sessions/<session_id>/tags", methods=["GET", "POST", "DELETE"])
def api_session_tags(session_id):
    """User-editable tags / bookmarks for a session (#56).

    GET    → {"tags": [...]}
    POST   {"tag": "x"}  → add, returns updated {"tags": [...]}
    DELETE {"tag": "x"}  → remove, returns updated {"tags": [...]}
    """
    if not validate_session_id(session_id):
        return jsonify({"error": "Invalid session ID"}), 400

    from ...storage import add_session_tag, get_session_tags, remove_session_tag

    if request.method == "GET":
        return jsonify({"tags": get_session_tags(_web.OUTPUT_DIR, session_id)})

    # Mutations are local-origin only, like the other POST/DELETE endpoints.
    if (err := _web._check_local_origin()) is not None:
        return err

    payload = request.get_json(silent=True)
    tag = (payload or {}).get("tag") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        return jsonify({"error": "a non-empty 'tag' is required"}), 400

    try:
        if request.method == "POST":
            tags = add_session_tag(_web.OUTPUT_DIR, session_id, tag)
        else:  # DELETE
            tags = remove_session_tag(_web.OUTPUT_DIR, session_id, tag)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    clear_index_cache()
    return jsonify({"tags": tags})


@bp.route("/api/tags")
def api_all_tags():
    """All user tags with session counts: {"tags": {tag: count, ...}}."""
    from ...storage import list_all_tags

    return jsonify({"tags": list_all_tags(_web.OUTPUT_DIR)})


@bp.route("/sessions/bulk-tag", methods=["POST"])
def sessions_bulk_tag():
    if (err := _web._check_local_origin()) is not None:
        return err
    session_ids, err_body = _web._validated_bulk_session_ids()
    if err_body is not None:
        return err_body

    tag = (request.form.get("tag") or "").strip()
    if not tag or not validate_search_param(tag):
        return "Invalid tag", 400

    from ...storage import add_session_tag

    for session_id in session_ids or []:
        try:
            add_session_tag(_web.OUTPUT_DIR, session_id, tag)
        except ValueError:
            return "Invalid tag", 400
    clear_index_cache()
    return _web._redirect_after_mutation()
