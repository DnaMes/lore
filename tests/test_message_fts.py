"""Message-level FTS index (migration 29): triggers, resumable backfill, evidence parity."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.storage import message_evidence_for_sessions, v2_db_path, write_sessions
from annuli.storage import schema as schema_module
from annuli.storage.fts import snippet_to_text
from annuli.storage.message_fts import (
    backfill_message_fts,
    message_fts_pending,
    message_fts_ready,
)
from annuli.storage.schema import apply_migrations, initialise, open_connection

T0 = datetime(2026, 3, 1, 9, 0, 0)


def _session(sid: str, bodies: list[str]) -> UnifiedSession:
    messages = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=body,
            timestamp=T0 + timedelta(minutes=i),
        )
        for i, body in enumerate(bodies)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=T0,
        last_updated=T0 + timedelta(minutes=len(bodies)),
        project_path="/repo/annuli",
        title=f"Session {sid}",
        messages=messages,
    )


def _fts_rowids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT rowid FROM message_fts")}


def test_empty_store_is_ready_at_once(tmp_path):
    conn = initialise(tmp_path / "v2.sqlite")
    assert message_fts_ready(conn)
    assert not message_fts_pending(conn)
    conn.close()


def test_triggers_index_insert_and_delete_and_cascade(tmp_path):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["alpha kwone", "beta"]), _session("b", ["gamma kwone"])])
    conn = open_connection(db)
    ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
    assert _fts_rowids(conn) == ids and len(ids) == 3
    # a rewrite (the sync path) replaces the rows: no stale fts rows may remain
    write_sessions(db, [_session("a", ["alpha kwone changed"])])
    ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
    assert _fts_rowids(conn) == ids
    # cascade delete from sessions fires the delete trigger
    conn.execute("DELETE FROM sessions WHERE id = 'b'")
    assert _fts_rowids(conn) == {r[0] for r in conn.execute("SELECT id FROM messages")}
    conn.close()


def _store_at_version_28(tmp_path):
    """A populated store whose migration 29 has not run yet."""
    db = v2_db_path(tmp_path)
    import annuli.storage.schema as sm

    original = sm.MIGRATIONS
    sm.MIGRATIONS = original[:28]
    try:
        write_sessions(db, [_session("a", ["alpha kwone", "beta kwone"]), _session("b", ["gamma"])])
    finally:
        sm.MIGRATIONS = original
    return db


def test_backfill_is_resumable_and_flips_ready(tmp_path, monkeypatch):
    db = _store_at_version_28(tmp_path)
    conn = open_connection(db)
    assert apply_migrations(conn) == schema_module.SCHEMA_VERSION
    assert message_fts_pending(conn) and not message_fts_ready(conn)
    assert _fts_rowids(conn) == set()

    monkeypatch.setattr("annuli.storage.message_fts.BATCH_IDS", 1)
    first = backfill_message_fts(conn, budget_seconds=1e-9)  # exhausted after one batch
    assert first == 1 and not message_fts_ready(conn)
    rest = backfill_message_fts(conn)
    assert first + rest == 3
    assert message_fts_ready(conn)
    assert _fts_rowids(conn) == {r[0] for r in conn.execute("SELECT id FROM messages")}
    assert backfill_message_fts(conn) == 0  # a finished backfill is a no-op
    conn.close()


def test_new_messages_during_backfill_are_not_indexed_twice(tmp_path):
    db = _store_at_version_28(tmp_path)
    conn = open_connection(db)
    apply_migrations(conn)
    write_sessions(db, [_session("c", ["delta kwone"])])  # trigger indexes it (id > upto)
    backfill_message_fts(conn)  # must not collide with that row
    assert message_fts_ready(conn)
    assert _fts_rowids(conn) == {r[0] for r in conn.execute("SELECT id FROM messages")}
    conn.close()


@pytest.mark.parametrize("ready", [True, False])
def test_evidence_is_identical_with_and_without_the_index(tmp_path, ready):
    db = _store_at_version_28(tmp_path)
    conn = open_connection(db)
    apply_migrations(conn)
    if ready:
        backfill_message_fts(conn)
    else:
        assert not message_fts_ready(conn)  # throwaway path
    conn.close()
    found = message_evidence_for_sessions(tmp_path, ["a", "b"], "kwone")
    assert set(found) == {"a"}
    hit = found["a"][0]
    assert hit["seq"] == 0 and hit["role"] == "user"
    assert "kwone" in snippet_to_text(hit["excerpt"])
    assert hit["truncated"] is False


def test_split_statements_keeps_trigger_bodies_whole():
    parts = schema_module._split_statements(
        "CREATE TABLE t(a); CREATE TRIGGER x AFTER INSERT ON t BEGIN "
        "INSERT INTO t VALUES (1); DELETE FROM t; END; SELECT 1"
    )
    assert len(parts) == 3
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].rstrip().endswith("END")
