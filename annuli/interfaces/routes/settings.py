"""``/settings`` route blueprint (read-only server-rendered overview)."""

import os
import sqlite3
from typing import Optional

from flask import Blueprint

from lore import __version__
from lore.interfaces import web as _web
from lore.services import index as _index_service
from lore.services import summarize_cli as _summarize_cli
from lore.storage import v2_db_path
from lore.storage.message_fts import message_fts_pending, message_fts_ready
from lore.storage.schema import open_connection

bp = Blueprint("settings", __name__)

_API_KEY_VARS = (
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def _search_state() -> dict[str, str]:
    """Return current search mode (hybrid/keyword) and message-index state."""
    build_info = _web._build_info_payload() or {}
    semantic = build_info.get("semantic") or {}
    is_hybrid = bool(
        _index_service._hybrid_enabled()
        and semantic.get("embeddings_available")
        and semantic.get("sqlite_vec_available")
    )
    mode = "hybrid" if is_hybrid else "keyword"

    db_path = v2_db_path(_web.INDEX_PATH.parent)
    message_index = "absent"
    if db_path.exists():
        try:
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
        except sqlite3.Error:
            message_index = "unavailable"

    return {
        "mode": mode,
        "message_index": message_index,
    }


def _ai_summary_state() -> dict[str, object]:
    """Return AI summary provider locality and key presence (never key values)."""
    raw_provider = os.environ.get("LORE_SUMMARY_PROVIDER", "").strip()
    provider: Optional[str] = raw_provider.lower() if raw_provider else None
    locality: Optional[str] = _summarize_cli.provider_locality(provider) if provider else None
    api_keys = [
        {
            "name": var_name,
            "is_set": bool(os.environ.get(var_name, "").strip()),
        }
        for var_name in _API_KEY_VARS
    ]
    return {
        "provider": provider or "not configured",
        "locality": locality,
        "api_keys": api_keys,
    }


@bp.route("/settings")
def settings_page():
    """Render the read-only Settings overview page."""
    return _web.render(
        "settings",
        active="settings",
        title="Settings",
        version=__version__,
        revision=_web._current_revision(),
        data_dir=str(_web.INDEX_PATH.parent),
        search_state=_search_state(),
        ai_summaries=_ai_summary_state(),
    )
