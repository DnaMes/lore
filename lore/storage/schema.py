"""v2 SQLite schema + migration runner (issue #44).

The schema is built up from a list of numbered, append-only migrations.
Each migration is one or more SQL statements; the runner records applied
versions in ``schema_version`` and skips anything already applied. This
keeps existing databases upgradeable forever and lets tests assert any
specific version.

Tier 1 — sessions and their messages (this PR).
Tier 2 — memory tables for agent-written knowledge (lands in PR 4).
Tier 3 — memory_sources linking memory back to the sessions it came from.

The unified FTS5 ``search_index`` covers Tier 1 from day one and gains
Tier 2/3 rows later without a schema change.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from .embeddings import EMBEDDING_DIM, load_vec_extension

logger = logging.getLogger(__name__)

# (version, description, sql) — append new migrations, NEVER edit existing ones.
MIGRATIONS: List[Tuple[int, str, str]] = [
    (
        1,
        "schema_version bookkeeping table",
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "tier 1: sessions table",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            tool          TEXT NOT NULL,
            project       TEXT,
            thread_id     TEXT,
            title         TEXT,
            created       TEXT NOT NULL,
            updated       TEXT NOT NULL,
            source_path   TEXT,
            source_mtime_ns INTEGER,
            git_branch    TEXT,
            git_commit    TEXT,
            cli_version   TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_tool      ON sessions(tool);
        CREATE INDEX IF NOT EXISTS idx_sessions_project   ON sessions(project);
        CREATE INDEX IF NOT EXISTS idx_sessions_thread    ON sessions(thread_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated   ON sessions(updated DESC);
        """,
    ),
    (
        3,
        "tier 1: messages table",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq        INTEGER NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            timestamp  TEXT,
            model      TEXT,
            tokens_json TEXT,
            UNIQUE(session_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session  ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_role     ON messages(role);
        CREATE INDEX IF NOT EXISTS idx_messages_session_seq
            ON messages(session_id, seq);
        """,
    ),
    (
        4,
        "unified FTS5 search index (sessions + messages today, memory in PR4)",
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            entity_type UNINDEXED,   -- 'session' | 'message' | 'memory' (PR4)
            entity_id   UNINDEXED,   -- TEXT id (sessions) or rowid (messages, memory)
            tool        UNINDEXED,
            project     UNINDEXED,
            title,
            body,
            tokenize='porter unicode61'
        );
        """,
    ),
    (
        5,
        "deletion tombstones (replaces the deleted_sessions.json file)",
        """
        CREATE TABLE IF NOT EXISTS deleted_sessions (
            session_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            reason     TEXT
        );
        """,
    ),
    (
        6,
        "denormalised display columns on sessions (avoids count joins on read)",
        """
        ALTER TABLE sessions ADD COLUMN messages_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN prompt_count   INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN prompt_outline TEXT;
        ALTER TABLE sessions ADD COLUMN export_path    TEXT;
        """,
    ),
    (
        7,
        "tier 2/3: agent memory — facts/decisions written by humans or agents",
        """
        CREATE TABLE IF NOT EXISTS memory (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT    NOT NULL,   -- fact|decision|todo|snippet|link|lesson
            title         TEXT    NOT NULL,
            body          TEXT    NOT NULL,
            scope_project TEXT,               -- NULL = global
            scope_tool    TEXT,               -- NULL = any tool
            author        TEXT,               -- 'human' | tool name
            created       TEXT    NOT NULL,
            updated       TEXT    NOT NULL,
            superseded_by INTEGER REFERENCES memory(id) ON DELETE SET NULL,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_kind    ON memory(kind);
        CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(scope_project);
        CREATE INDEX IF NOT EXISTS idx_memory_active  ON memory(superseded_by);

        CREATE TABLE IF NOT EXISTS memory_tags (
            memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            tag       TEXT    NOT NULL,
            PRIMARY KEY (memory_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);

        -- session_id / message_id are loose references, NOT foreign keys:
        -- a memory may be linked to a session before that session is synced
        -- into the v2 store (or to one extracted on a different machine).
        -- Enforcing an FK at insert time would reject those valid links.
        CREATE TABLE IF NOT EXISTS memory_sources (
            memory_id  INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            session_id TEXT,
            message_id INTEGER,
            note       TEXT,
            PRIMARY KEY (memory_id, session_id, message_id)
        );
        """,
    ),
    (
        8,
        "track whether a session's message rows are present in v2",
        # Incremental sync writes metadata-only rows for unchanged sessions
        # (no message rows). messages_synced = 0 marks such rows so readers
        # and a future backfill can tell complete sessions from partial ones.
        """
        ALTER TABLE sessions ADD COLUMN messages_synced INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        9,
        "store-level metadata (generated_at) for staleness detection",
        # A key/value side table. 'generated_at' is stamped on every write so
        # readers can tell whether the v2 store is fresh relative to the
        # legacy index.json.
        """
        CREATE TABLE IF NOT EXISTS store_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """,
    ),
    (
        10,
        "memory embeddings for semantic recall (optional 'semantic' extra)",
        # One embedding vector per memory. Stored as a raw float32 BLOB —
        # cosine similarity over a few hundred rows in Python is fast enough,
        # so no native vector-search extension is required. `model` records
        # which embedding model produced the vector so a model change can be
        # detected and the vectors re-computed.
        """
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id INTEGER PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
            model     TEXT    NOT NULL,
            dim       INTEGER NOT NULL,
            vector    BLOB    NOT NULL,
            created   TEXT    NOT NULL
        );
        """,
    ),
    (
        11,
        "per-session total token count, for the cost dashboard (#67)",
        # Aggregated input+output tokens for the session. Defaults to 0 so old
        # rows and tools without usage data report no cost rather than NULL.
        """
        ALTER TABLE sessions ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        12,
        "user-editable session tags / bookmarks (#56)",
        # session_id is a LOOSE reference, NOT a foreign key: a user may tag a
        # session before it is synced into the v2 store (or one extracted on a
        # different machine), so an FK would reject valid tags. The tags survive
        # index rebuilds (unlike the LLM-generated tags in the flat index).
        # "bookmark" is just a reserved tag value the UI can treat specially.
        """
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag        TEXT NOT NULL,
            created    TEXT NOT NULL,
            PRIMARY KEY (session_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);
        """,
    ),
    (
        13,
        "embedding bookkeeping for incremental session vectors (#95)",
        # Sidecar for the vec0 ``session_embeddings`` table (which itself must
        # stay OUT of this list — see ensure_session_vec_table). Records which
        # source state each stored vector was computed from, so a reload only
        # re-embeds sessions whose source_mtime_ns (or model) changed instead
        # of re-embedding the whole archive — the full pass blew the web
        # reload job's 180s timeout. A plain table: no vec0 module needed.
        """
        CREATE TABLE IF NOT EXISTS session_embedding_meta (
            session_id      TEXT PRIMARY KEY,
            source_mtime_ns INTEGER,
            model           TEXT NOT NULL,
            created         TEXT NOT NULL
        );
        """,
    ),
]


def open_connection(path: Path) -> sqlite3.Connection:
    """Open a connection with PRAGMAs sensible for our workload.

    - ``isolation_level=None`` puts the connection in autocommit mode, which
      we want so that PRAGMAs run immediately and ``executescript()`` can
      contain DDL without colliding with Python's implicit BEGIN. The
      migration runner brackets each step in its own explicit BEGIN/COMMIT.
    - WAL gives concurrent readers + a writer (web UI reads while syncs run).
    - foreign_keys must be enabled per-connection — SQLite default is OFF.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    # The v2 DB holds full session transcripts — restrict to the owner (#41).
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout makes a concurrent writer wait instead of failing
    # immediately with SQLITE_BUSY — the web UI reads while a sync writes,
    # and a memory write can overlap a sync (#40).
    conn.execute("PRAGMA busy_timeout = 5000")
    # Best-effort: load sqlite-vec so the ``session_embeddings`` vec0 table is
    # usable on this connection. No-op (returns False) when the optional
    # dependency or the host's extension-loading support is absent — vector
    # search then degrades to FTS-only. The table itself is created lazily by
    # ``ensure_session_vec_table`` (NOT a migration — a vec0 CREATE would raise
    # 'no such module' and wedge the migration runner where the extension is
    # missing).
    load_vec_extension(conn)
    return conn


def ensure_session_vec_table(conn: sqlite3.Connection) -> bool:
    """Create the ``session_embeddings`` vec0 table if sqlite-vec is loaded.

    Returns True if the table exists (or was just created), False if the
    extension isn't available on this connection — in which case the caller
    should skip vector writes/search and rely on FTS. Idempotent:
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` is a no-op once the table exists.

    Kept out of the linear ``MIGRATIONS`` list on purpose: a ``vec0`` CREATE
    fails with 'no such module: vec0' when the extension can't load, which
    would abort ``apply_migrations`` and leave the DB stuck below its target
    version on any host without sqlite-vec.
    """
    if not load_vec_extension(conn):
        return False
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS session_embeddings USING vec0("
            "session_id TEXT PRIMARY KEY, "
            "model TEXT, "
            f"embedding float[{EMBEDDING_DIM}]"
            ")"
        )
        return True
    except sqlite3.Error as exc:
        logger.warning("session_embeddings vec0 table unavailable: %s", exc)
        return False


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version (0 on fresh DB)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not row:
        return 0
    result = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(result[0])


# Matches "ALTER TABLE <tbl> ADD COLUMN <col> ..." (case-insensitive),
# capturing the table and column names.
_ALTER_ADD_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\b",
    re.IGNORECASE,
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    """Return the set of column names on ``table`` (empty if it doesn't exist)."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _run_migration_sql(conn: sqlite3.Connection, sql: str) -> None:
    """Execute migration DDL, skipping ALTER ADD COLUMN for existing columns.

    ``CREATE TABLE IF NOT EXISTS`` is naturally idempotent, but
    ``ALTER TABLE ADD COLUMN`` is not — re-running it raises 'duplicate
    column name'. If a prior migration attempt half-applied (added the
    column but never recorded the version), re-running would wedge the
    runner. So each ADD COLUMN is checked against the live schema first.
    """
    # Split on ';' — migration SQL only ever contains simple statements.
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for statement in statements:
        match = _ALTER_ADD_COLUMN.search(statement)
        if match:
            table, column = match.group(1), match.group(2)
            if column in _table_columns(conn, table):
                continue  # column already present — skip, idempotent
        conn.execute(statement)


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply every pending migration in order. Returns the new current version.

    Each migration runs inside its own transaction; a failing migration rolls
    back and re-raises, leaving the DB at the last successfully applied
    version. Idempotent — running it twice is a no-op, and a half-applied
    ALTER TABLE migration can be safely re-run.
    """
    applied = current_version(conn)
    for version, description, sql in MIGRATIONS:
        if version <= applied:
            continue
        # Run the DDL first (autocommit, ALTER-idempotent), then record the
        # bookkeeping insert in its own short transaction. executescript's
        # implicit COMMIT would otherwise close a BEGIN started here.
        try:
            _run_migration_sql(conn, sql)
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO schema_version(version, description, applied_at) VALUES (?, ?, ?)",
                (
                    version,
                    description,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            # Best-effort rollback; if no tx is open this is a noop in autocommit.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        applied = version
    return applied


# Public helper for callers that want a ready-to-use DB in one call.
def initialise(path: Path) -> sqlite3.Connection:
    """Open ``path`` and apply every pending migration. Returns the connection."""
    conn = open_connection(path)
    apply_migrations(conn)
    # Create the optional vector table once the base schema is in place.
    # No-op when sqlite-vec is unavailable — search stays FTS-only.
    ensure_session_vec_table(conn)
    return conn


def open_v2_connection(db_path: Path) -> sqlite3.Connection:
    """Open the v2 store, migrating only when the schema is behind (#128).

    ``initialise`` re-runs the full migration bookkeeping on every connect
    (``current_version`` lookup, migration loop, vec-table ensure). For
    high-frequency single-row callers — every tag/memory operation opens a
    fresh connection — that fixed overhead dominates the actual work once
    the schema is current. This variant pays the check once and takes the
    fast path afterwards; a behind-version DB still migrates exactly like
    ``initialise``.
    """
    conn = open_connection(db_path)
    if current_version(conn) < len(MIGRATIONS):
        apply_migrations(conn)
        # Create the optional vector table once the base schema is in
        # place. No-op when sqlite-vec is unavailable — FTS-only search.
        ensure_session_vec_table(conn)
    return conn
