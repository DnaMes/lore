"""Persistent storage layer — SQLite as the single source of truth (issue #44).

This package owns the v2 SQLite schema that supersedes the flat
``index.json`` + parallel ``index.sqlite`` (FTS-only) split. The migration
is staged across multiple PRs:

  PR 1 (this) — Schema + migration runner. No behaviour change yet.
  PR 2       — Dual-write sessions/messages into v2 alongside the JSON index.
  PR 3       — Readers switch to v2; JSON becomes an optional export.
  PR 4       — Memory tables + MCP write/recall tools (the agent-memory vision).

Public surface:
    open_connection(path)  -> sqlite3.Connection
    apply_migrations(conn) -> int   # highest version applied
    current_version(conn)  -> int
"""

from __future__ import annotations

from .embeddings import embed_text, embeddings_available
from .feedback import feedback_influence, feedback_weight, ranking_factor, record_feedback
from .fusion import rrf_merge
from .knowledge import (
    get_session_knowledge,
    list_session_knowledge,
    prompt_fingerprint,
    save_session_knowledge,
)
from .memory import (
    MEMORY_KINDS,
    MemoryEntry,
    add_memory,
    delete_memory,
    get_memory,
    link_memory_to_session,
    list_memory,
    list_memory_sources,
    recall_memory,
    supersede_memory,
)
from .reader import load_index_v2, load_session_messages_v2, v2_is_available
from .schema import apply_migrations, current_version, initialise, open_connection
from .search import search_sessions, semantic_search_sessions
from .tags import (
    BOOKMARK_TAG,
    add_session_tag,
    get_session_tags,
    get_tags_for_sessions,
    list_all_tags,
    remove_session_tag,
)
from .writer import v2_db_path, write_sessions, write_sessions_safe

__all__ = [
    "BOOKMARK_TAG",
    "MEMORY_KINDS",
    "MemoryEntry",
    "add_memory",
    "add_session_tag",
    "apply_migrations",
    "current_version",
    "delete_memory",
    "embed_text",
    "embeddings_available",
    "feedback_influence",
    "feedback_weight",
    "get_memory",
    "get_session_knowledge",
    "get_session_tags",
    "get_tags_for_sessions",
    "initialise",
    "link_memory_to_session",
    "list_all_tags",
    "list_memory",
    "list_memory_sources",
    "list_session_knowledge",
    "load_index_v2",
    "load_session_messages_v2",
    "open_connection",
    "prompt_fingerprint",
    "ranking_factor",
    "recall_memory",
    "record_feedback",
    "remove_session_tag",
    "rrf_merge",
    "save_session_knowledge",
    "search_sessions",
    "semantic_search_sessions",
    "supersede_memory",
    "v2_db_path",
    "v2_is_available",
    "write_sessions",
    "write_sessions_safe",
]
