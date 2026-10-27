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

from .backup import (
    BackupError,
    BackupManifest,
    BackupResult,
    BundleReport,
    RestoreReport,
    create_backup,
    restore_backup,
    verify_bundle,
)
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
    CLAIM_STATES,
    CLAIM_TYPES,
    MEMORY_KINDS,
    MemoryEntry,
    add_memory,
    claim_state,
    delete_memory,
    find_similar_memories,
    get_memory,
    link_memory_to_session,
    list_memory,
    list_memory_sources,
    memory_index_health,
    recall_memory,
    record_claim,
    repair_memory_index,
    set_memory_status,
    supersede_memory,
)
from .portable import (
    ImportReport,
    PortableExport,
    PortableManifest,
    PortableReport,
    create_portable_bundle,
    import_bundle,
    verify_portable_bundle,
)
from .reader import (
    load_index_v2,
    load_session_message_window,
    load_session_messages_v2,
    v2_is_available,
)
from .schema import apply_migrations, current_version, initialise, open_connection
from .search import (
    count_session_hits,
    message_evidence_for_sessions,
    search_sessions,
    semantic_search_sessions,
)
from .tags import (
    BOOKMARK_TAG,
    add_session_tag,
    all_tags_by_session,
    get_session_tags,
    get_tags_for_sessions,
    list_all_tags,
    remove_session_tag,
)
from .writer import v2_db_path, write_sessions, write_sessions_safe

__all__ = [
    "BOOKMARK_TAG",
    "MEMORY_KINDS",
    "BackupError",
    "BackupManifest",
    "BackupResult",
    "BundleReport",
    "MemoryEntry",
    "ImportReport",
    "PortableExport",
    "PortableManifest",
    "PortableReport",
    "RestoreReport",
    "add_memory",
    "add_session_tag",
    "all_tags_by_session",
    "apply_migrations",
    "create_backup",
    "create_portable_bundle",
    "current_version",
    "delete_memory",
    "embed_text",
    "embeddings_available",
    "feedback_influence",
    "feedback_weight",
    "find_similar_memories",
    "get_memory",
    "get_session_knowledge",
    "get_session_tags",
    "get_tags_for_sessions",
    "import_bundle",
    "initialise",
    "link_memory_to_session",
    "list_all_tags",
    "list_memory",
    "CLAIM_STATES",
    "CLAIM_TYPES",
    "claim_state",
    "memory_index_health",
    "record_claim",
    "list_memory_sources",
    "list_session_knowledge",
    "load_index_v2",
    "load_session_message_window",
    "load_session_messages_v2",
    "open_connection",
    "prompt_fingerprint",
    "ranking_factor",
    "recall_memory",
    "repair_memory_index",
    "record_feedback",
    "remove_session_tag",
    "restore_backup",
    "rrf_merge",
    "save_session_knowledge",
    "count_session_hits",
    "message_evidence_for_sessions",
    "count_session_hits",
    "search_sessions",
    "semantic_search_sessions",
    "set_memory_status",
    "supersede_memory",
    "v2_db_path",
    "verify_bundle",
    "verify_portable_bundle",
    "v2_is_available",
    "write_sessions",
    "write_sessions_safe",
]
