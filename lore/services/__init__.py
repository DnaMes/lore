"""Interface-agnostic service layer.

This package holds shared, framework-free logic that both the web
interface (``lore.interfaces.web``) and the MCP server
(``lore.interfaces.mcp``) depend on.

It exists to kill code duplication (the "iterate extractors -> build
index" routine used to be copied into ~4 places) and to fix a layering
inversion: the MCP server previously imported from ``web_data``, but the
web app and the MCP server are *sibling* interfaces and neither should
depend on the other. Shared logic belongs here instead.

Nothing in this package may import Flask or any ``lore.interfaces``
module.
"""

from .cache import threadsafe_lru_cache
from .extraction import (
    ActionJobCancelledError,
    build_index_if_missing,
    build_search_index,
    collect_sessions,
    find_live_session,
    select_extractors,
)
from .index import (
    annotate_display_titles,
    apply_deleted_filter,
    apply_deleted_filter_to_results,
    clear_index_cache,
    load_deleted_session_ids,
    load_index,
    load_index_summary,
    remember_deleted_session_id,
    save_deleted_session_ids,
    search_index,
)

__all__ = [
    "threadsafe_lru_cache",
    "ActionJobCancelledError",
    "build_index_if_missing",
    "build_search_index",
    "collect_sessions",
    "find_live_session",
    "select_extractors",
    "annotate_display_titles",
    "apply_deleted_filter",
    "apply_deleted_filter_to_results",
    "clear_index_cache",
    "load_deleted_session_ids",
    "load_index",
    "load_index_summary",
    "remember_deleted_session_id",
    "save_deleted_session_ids",
    "search_index",
]
