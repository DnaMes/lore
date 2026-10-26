"""``/memory``, ``/noise-rules`` and their JSON endpoints (#119)."""

from flask import Blueprint, jsonify, redirect, request

from lore.interfaces import web as _web
from lore.utils.security import validate_search_query
from lore.utils.tooling import normalize_tool_name

from ..web_data import clear_index_cache, clear_sessions_cache
from ..web_services import preview_normalize_message
from ..web_utils import _noise_rule_presets, save_noise_rules

bp = Blueprint("memory", __name__)


@bp.route("/memory")
def memory_page():
    """Browse and search the shared agent-memory store (#33)."""
    from lore.storage import (
        MEMORY_KINDS,
        embeddings_available,
        list_memory,
        recall_memory,
    )

    query = request.args.get("q", "").strip()
    kind = request.args.get("kind", "").strip() or None
    semantic = request.args.get("semantic", "") in ("1", "true", "on")
    # (#197) Candidates are a review queue: hidden from the default list, shown
    # on their own tab with accept/reject actions.
    status = request.args.get("status", "").strip().lower()
    if status not in ("candidate", "confirmed"):
        status = "confirmed"

    if query and not validate_search_query(query):
        return "Invalid search query", 400
    if kind and kind not in MEMORY_KINDS:
        return "Invalid kind", 400

    output_dir = _web.INDEX_PATH.parent
    if query:
        entries = recall_memory(
            output_dir,
            query,
            kind=kind,
            limit=100,
            semantic=True if semantic else None,
            status=status,
        )
    else:
        entries = list_memory(output_dir, kind=kind, limit=100, status=status)

    memories = [entry.to_dict() for entry in entries]

    return _web.render(
        "memory",
        active="memory",
        memories=memories,
        total=len(entries),
        query=query,
        kind=kind,
        kinds=list(MEMORY_KINDS),
        semantic=semantic,
        semantic_available=embeddings_available(),
        status=status,
        candidate_count=len(list_memory(output_dir, kind=kind, limit=500, status="candidate")),
        confirmed_count=len(list_memory(output_dir, kind=kind, limit=500, status="confirmed")),
        title="Memory",
    )


@bp.route("/memory/<int:memory_id>/review", methods=["POST"])
def memory_review(memory_id):
    """Accept or reject a memory candidate (#197).

    Rejecting keeps the row (status ``rejected``), so the decision is reversible
    with `lore memory review` or a direct status change; nothing is deleted.
    """
    if (err := _web._check_local_origin()) is not None:
        return err
    from lore.storage import set_memory_status

    action = (request.form.get("action") or request.args.get("action") or "").strip().lower()
    if action not in ("accept", "reject"):
        return "Invalid review action", 400
    target = "confirmed" if action == "accept" else "rejected"
    try:
        set_memory_status(_web.INDEX_PATH.parent, memory_id, target, from_status="candidate")
    except ValueError:
        return "Invalid review action", 400
    return redirect("/memory?status=candidate")


@bp.route("/memory/<int:memory_id>/delete", methods=["POST"])
def memory_delete(memory_id):
    if (err := _web._check_local_origin()) is not None:
        return err
    from lore.storage import delete_memory

    delete_memory(_web.INDEX_PATH.parent, memory_id)
    return redirect("/memory")


@bp.route("/noise-rules")
def noise_rules_page():
    return _web.render(
        "noise_rules",
        active="noise-rules",
        noise_rules=_web.load_noise_rules(),
        noise_rule_presets=_noise_rule_presets(),
        title="Noise Rules",
    )


@bp.route("/api/noise-rules", methods=["GET", "POST"])
def api_noise_rules():
    if request.method == "POST":
        if (err := _web._check_local_origin()) is not None:
            return err
    if request.method == "GET":
        return jsonify(_web.load_noise_rules())

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload"}), 400
    rules = save_noise_rules(payload)
    clear_sessions_cache()
    clear_index_cache()
    return jsonify({"status": "ok", "rules": rules})


@bp.route("/api/noise-rules/preview", methods=["POST"])
def api_noise_rules_preview():
    if (err := _web._check_local_origin()) is not None:
        return err
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload"}), 400

    tool = normalize_tool_name(str(payload.get("tool") or "").strip()) or ""
    role = str(payload.get("role") or "assistant").strip().lower()
    content = str(payload.get("content") or "")
    provided_rules = payload.get("noise_rules")
    if provided_rules is None:
        rules = _web.load_noise_rules()
    elif isinstance(provided_rules, dict):
        merged = _web.load_noise_rules()
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
