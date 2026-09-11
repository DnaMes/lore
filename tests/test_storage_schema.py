"""Tests for the v2 SQLite schema + migration runner (issue #44, PR 1).

This is the foundation PR — no behaviour change anywhere else yet. The
tests assert the schema, the migration runner's idempotency, FK cascade,
WAL mode, and FTS5 wiring.
"""

from __future__ import annotations

import sqlite3

import pytest

from lore.storage import (
    apply_migrations,
    current_version,
    initialise,
    open_connection,
)
from lore.storage import schema as schema_module
from lore.storage.memory import add_memory
from lore.storage.schema import MIGRATIONS, open_v2_connection
from lore.storage.tags import add_session_tag, get_session_tags

# ---------------------------------------------------------------------------
# Connection PRAGMAs
# ---------------------------------------------------------------------------


def test_open_connection_creates_parent_dirs(tmp_path):
    db = tmp_path / "nested" / "deeper" / "v2.sqlite"
    conn = open_connection(db)
    assert db.parent.is_dir()
    conn.close()


def test_open_connection_enables_wal(tmp_path):
    conn = open_connection(tmp_path / "v2.sqlite")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_open_connection_enables_foreign_keys(tmp_path):
    conn = open_connection(tmp_path / "v2.sqlite")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def test_fresh_database_starts_at_version_zero(tmp_path):
    conn = open_connection(tmp_path / "v2.sqlite")
    assert current_version(conn) == 0


def test_apply_migrations_reaches_highest_known_version(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    expected = max(v for v, _, _ in MIGRATIONS)
    assert current_version(conn) == expected


def test_apply_migrations_is_idempotent(tmp_path):
    db = tmp_path / "v2.sqlite"
    conn1 = initialise(db)
    v1 = current_version(conn1)
    rows1 = conn1.execute(
        "SELECT version, description FROM schema_version ORDER BY version"
    ).fetchall()
    conn1.close()

    # Re-open and re-apply: must be a no-op.
    conn2 = open_connection(db)
    v2 = apply_migrations(conn2)
    rows2 = conn2.execute(
        "SELECT version, description FROM schema_version ORDER BY version"
    ).fetchall()
    assert v2 == v1
    assert [tuple(r) for r in rows2] == [tuple(r) for r in rows1]


def test_each_migration_is_recorded(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [v for v, _, _ in MIGRATIONS]


# ---------------------------------------------------------------------------
# Schema shape — locks in the v2 surface
# ---------------------------------------------------------------------------


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','virtual table') AND name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE 'search_index_%' "
        # vec0 creates several shadow tables (session_embeddings_chunks,
        # _rowids, _vector_chunks00, …) — only the virtual table itself is ours.
        "  AND name NOT LIKE 'session_embeddings_%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_expected_tables_exist(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    core = {
        "schema_version",
        "sessions",
        "messages",
        "search_index",
        "deleted_sessions",
        "memory",
        "memory_tags",
        "memory_sources",
        "store_meta",
        "memory_embeddings",
        "session_tags",
        "session_embedding_meta",
    }
    tables = _tables(conn)
    # The vec0 session_embeddings table only exists when sqlite-vec loaded;
    # it is created outside the migration list, so drop it before comparing.
    assert tables - {"session_embeddings"} == core


def test_session_embeddings_table_present_with_vec(tmp_path):
    from lore.storage.embeddings import sqlite_vec_available

    conn = initialise(tmp_path / "v2.sqlite")
    present = "session_embeddings" in _tables(conn)
    # Table exists iff the optional extension is available in this env.
    assert present == sqlite_vec_available()


def test_sessions_columns(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    expected = {
        "id",
        "tool",
        "project",
        "thread_id",
        "title",
        "created",
        "updated",
        "source_path",
        "source_mtime_ns",
        "git_branch",
        "git_commit",
        "cli_version",
        "metadata_json",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_messages_columns(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    expected = {
        "id",
        "session_id",
        "seq",
        "role",
        "content",
        "timestamp",
        "model",
        "tokens_json",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_sessions_indexes(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    names = {
        r["name"]
        for r in conn.execute("PRAGMA index_list(sessions)")
        if not r["name"].startswith("sqlite_")
    }
    for expected in (
        "idx_sessions_tool",
        "idx_sessions_project",
        "idx_sessions_thread",
        "idx_sessions_updated",
    ):
        assert expected in names


# ---------------------------------------------------------------------------
# Behavioural guarantees
# ---------------------------------------------------------------------------


def test_session_messages_cascade_on_delete(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    conn.execute(
        "INSERT INTO sessions(id,tool,created,updated) VALUES(?,?,?,?)",
        ("s1", "claude-code", "2026-01-01", "2026-01-01"),
    )
    for seq in range(3):
        conn.execute(
            "INSERT INTO messages(session_id,seq,role,content) VALUES(?,?,?,?)",
            ("s1", seq, "user", f"msg {seq}"),
        )
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3

    conn.execute("DELETE FROM sessions WHERE id='s1'")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_messages_unique_per_session_seq(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    conn.execute("INSERT INTO sessions(id,tool,created,updated) VALUES('s1','x','t','t')")
    conn.execute("INSERT INTO messages(session_id,seq,role,content) VALUES('s1',0,'u','a')")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO messages(session_id,seq,role,content) VALUES('s1',0,'u','b')")


def test_search_index_finds_inserted_row(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    conn.execute(
        "INSERT INTO search_index(entity_type,entity_id,tool,project,title,body) "
        "VALUES('session','s1','claude-code','/p','hello world','quick brown fox')"
    )
    hits = conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'brown'"
    ).fetchall()
    assert [h[0] for h in hits] == ["s1"]


def test_search_index_porter_stemming_works(tmp_path):
    """The porter tokenizer matches 'jumped' against the query 'jump'."""
    conn = initialise(tmp_path / "v2.sqlite")
    conn.execute(
        "INSERT INTO search_index(entity_type,entity_id,title,body) "
        "VALUES('session','s1','t','the fox jumped over')"
    )
    hits = conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'jump'"
    ).fetchall()
    assert [h[0] for h in hits] == ["s1"]


def test_deleted_sessions_table_writable(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    conn.execute(
        "INSERT INTO deleted_sessions(session_id, deleted_at, reason) "
        "VALUES('s1','2026-01-01','user removed')"
    )
    row = conn.execute("SELECT session_id, reason FROM deleted_sessions").fetchone()
    assert row["session_id"] == "s1"
    assert row["reason"] == "user removed"


# ---------------------------------------------------------------------------
# Forward-compat guard: never edit an existing migration
# ---------------------------------------------------------------------------


def test_migration_versions_are_unique_and_sorted():
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions == sorted(versions), "MIGRATIONS must be in ascending order"
    assert len(versions) == len(set(versions)), "duplicate migration version"


# ---------------------------------------------------------------------------
# Fast-path connection for high-frequency callers (#128)
# ---------------------------------------------------------------------------


def test_open_v2_connection_migrates_fresh_db(tmp_path):
    db = tmp_path / "v2.sqlite"
    conn = open_v2_connection(db)
    try:
        assert current_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()


def test_open_v2_connection_skips_migration_bookkeeping_on_current_schema(tmp_path, monkeypatch):
    """A schema-current DB must not re-run the migration machinery (#128).

    Tag/memory operations open a fresh connection per call; the whole point
    of the fast path is that the steady-state connect pays no migration
    bookkeeping. Prove it by failing loudly if apply_migrations is entered.
    """
    db = tmp_path / "v2.sqlite"
    first = open_v2_connection(db)
    first.close()

    def _must_not_run(conn):
        raise AssertionError("apply_migrations must not run on a current schema")

    monkeypatch.setattr(schema_module, "apply_migrations", _must_not_run)

    conn = open_v2_connection(db)
    try:
        assert current_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()


def test_open_v2_connection_migrates_behind_version_db(tmp_path):
    """A DB at an older schema version still migrates (parity with initialise)."""
    db = tmp_path / "v2.sqlite"
    conn = open_connection(db)
    # Fabricate a "one migration behind" state by stamping the first version.
    first_version = MIGRATIONS[0][0]
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version(version, description, applied_at) VALUES (?, 'seed', 'now')",
        (first_version,),
    )
    conn.close()

    conn = open_v2_connection(db)
    try:
        assert current_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()


def test_tags_and_memory_ops_run_through_fast_path(tmp_path):
    """End-to-end: tag + memory writes work via the fast-path connect."""
    output_dir = tmp_path / "out"
    tags = add_session_tag(output_dir, "ses-1", "bookmark")
    assert tags == ["bookmark"]
    memory_id = add_memory(
        output_dir,
        kind="decision",
        title="Use fast path",
        body="Single-row ops must not pay full migration bookkeeping.",
    )
    assert memory_id >= 1
    assert get_session_tags(output_dir, "ses-1") == ["bookmark"]
