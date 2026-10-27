"""Per-message vector population (#190).

``embed_messages`` syncs the vec0 ``message_embeddings`` table (+ its
``message_embedding_meta`` sidecar, migration 25, content hash in migration 28)
to the store's messages. It is store-driven and incremental by
``(session_id, seq, model, content)``, and degrades to a no-op (0 vectors, no
crash) when sqlite-vec or fastembed is unavailable. The embedder is stubbed in
every test, so no model is downloaded.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import message_vectors, v2_db_path, write_sessions
from lore.storage import schema as schema_module
from lore.storage.embeddings import EMBEDDING_DIM, sqlite_vec_available
from lore.storage.message_vectors import embed_messages, message_key, pending_messages
from lore.storage.schema import MIGRATIONS, apply_migrations, initialise

requires_vec = pytest.mark.skipif(
    not sqlite_vec_available(),
    reason="optional 'semantic' extra (sqlite-vec) not installed",
)


def _session(sid: str, contents: list[str], title: str = "Sample") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title=title,
        messages=[
            UnifiedMessage(
                role=Role.USER if seq % 2 == 0 else Role.ASSISTANT,
                content=content,
                timestamp=datetime(2026, 1, 1, 12, seq),
            )
            for seq, content in enumerate(contents)
        ],
    )


@pytest.fixture
def store(tmp_path) -> Path:
    """A v2 store with two sessions; the session vector pass is stubbed out."""
    db = v2_db_path(tmp_path)
    write_sessions(
        db, [_session("a", ["first body", "second body"]), _session("b", ["third body"])]
    )
    return db


@pytest.fixture
def fake_embedder(monkeypatch):
    """Stub the model: every text maps to one constant non-zero vector."""
    calls: list[str] = []

    def fake(text: str) -> list[float]:
        calls.append(text)
        return [1.0] * EMBEDDING_DIM

    monkeypatch.setattr(message_vectors, "embeddings_available", lambda: True)
    monkeypatch.setattr(message_vectors, "embed_text", fake)
    return calls


def _conn(db: Path):
    return initialise(db)


def _vec_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]


def _meta_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM message_embedding_meta").fetchone()[0]


def test_message_key_is_the_permalink_identity():
    assert message_key("sess-1", 7) == "sess-1#m7"
    assert message_key("claude-code:laptop:sess-1", 0) == "claude-code:laptop:sess-1#m0"


@requires_vec
def test_embeds_every_message_once(store, fake_embedder):
    conn = _conn(store)
    try:
        assert len(pending_messages(conn)) == 3
        assert embed_messages(conn, budget_seconds=0) == 3
        assert _vec_count(conn) == 3
        assert _meta_count(conn) == 3
        assert {row[0] for row in conn.execute("SELECT message_key FROM message_embeddings")} == {
            message_key("a", 0),
            message_key("a", 1),
            message_key("b", 0),
        }
    finally:
        conn.close()


@requires_vec
def test_second_run_is_incremental(store, fake_embedder):
    conn = _conn(store)
    try:
        embed_messages(conn, budget_seconds=0)
        assert pending_messages(conn) == []
        assert embed_messages(conn, budget_seconds=0) == 0
        assert fake_embedder and len(fake_embedder) == 3  # the model was not called again
    finally:
        conn.close()


@requires_vec
def test_a_model_change_reembeds(tmp_path, fake_embedder):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["one body", "two body"])])
    conn = initialise(db)
    try:
        embed_messages(conn, budget_seconds=0)
        conn.execute("UPDATE message_embedding_meta SET model = 'an-older-model'")
        assert len(pending_messages(conn)) == 2
        assert embed_messages(conn, budget_seconds=0) == 2
        assert {
            row[0] for row in conn.execute("SELECT DISTINCT model FROM message_embedding_meta")
        } == {message_vectors.DEFAULT_MODEL}
    finally:
        conn.close()


@requires_vec
def test_empty_bodies_are_skipped(tmp_path, fake_embedder):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["real body", "   ", ""])])
    conn = initialise(db)
    try:
        assert embed_messages(conn, budget_seconds=0) == 1
        assert _vec_count(conn) == 1
    finally:
        conn.close()


@requires_vec
def test_budget_stop_resumes_on_the_next_run(tmp_path, fake_embedder):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["one", "two", "three"])])
    conn = initialise(db)
    try:
        assert embed_messages(conn, budget_seconds=1e-9) == 0
        assert _vec_count(conn) == 0
        assert embed_messages(conn, budget_seconds=0) == 3  # picks up where it stopped
        assert _vec_count(conn) == 3
    finally:
        conn.close()


@requires_vec
def test_updates_replace_rather_than_duplicate(tmp_path, fake_embedder):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["one body"])])
    conn = initialise(db)
    try:
        embed_messages(conn, budget_seconds=0)
        conn.execute("UPDATE message_embedding_meta SET model = 'other'")
        assert embed_messages(conn, budget_seconds=0) == 1
        assert _vec_count(conn) == 1  # vec0 has no OR REPLACE: DELETE + INSERT
        assert _meta_count(conn) == 1
    finally:
        conn.close()


def test_noop_without_embeddings(store, monkeypatch):
    monkeypatch.setattr(message_vectors, "embeddings_available", lambda: False)
    conn = _conn(store)
    try:
        assert embed_messages(conn, budget_seconds=0) == 0
    finally:
        conn.close()


def test_noop_without_vec_table(store, monkeypatch):
    monkeypatch.setattr(message_vectors, "embeddings_available", lambda: True)
    monkeypatch.setattr(message_vectors, "ensure_message_vec_table", lambda conn: False)
    conn = _conn(store)
    try:
        assert embed_messages(conn, budget_seconds=0) == 0
    finally:
        conn.close()


@requires_vec
def test_unchanged_content_stays_not_pending(store, fake_embedder):
    """A re-run over untouched messages must not re-embed anything."""
    conn = _conn(store)
    try:
        embed_messages(conn, budget_seconds=0)
        assert pending_messages(conn) == []
        assert fake_embedder == ["first body", "second body", "third body"]
    finally:
        conn.close()


@requires_vec
def test_changed_content_becomes_pending_and_is_reembedded(store, fake_embedder):
    """The writer replaces message bodies without touching the meta sidecar.

    The stored content hash must go stale, so the changed message is scored
    against a vector of its new text, not the old one.
    """
    conn = _conn(store)
    try:
        embed_messages(conn, budget_seconds=0)
        assert pending_messages(conn) == []
        conn.execute(
            "UPDATE messages SET content = 'brand new body' WHERE session_id = 'a' AND seq = 0"
        )
        pending = pending_messages(conn)
        assert [(p[0], p[1]) for p in pending] == [("a", 0)]
        assert embed_messages(conn, budget_seconds=0) == 1
        assert pending_messages(conn) == []
        assert fake_embedder[-1] == "brand new body"
        # The vector row was replaced, not duplicated.
        assert _vec_count(conn) == 3
        assert _meta_count(conn) == 3
        stored_hash = conn.execute(
            "SELECT content_hash FROM message_embedding_meta WHERE message_key = ?",
            (message_key("a", 0),),
        ).fetchone()[0]
        assert stored_hash == message_vectors._content_hash("brand new body")
    finally:
        conn.close()


@requires_vec
def test_legacy_row_without_hash_is_reembedded_exactly_once(tmp_path, fake_embedder):
    """Pre-migration-28 rows (content_hash NULL) count as pending once."""
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", ["one body", "two body"])])
    conn = initialise(db)
    try:
        embed_messages(conn, budget_seconds=0)
        # Simulate the legacy state: vectors exist, hashes do not.
        conn.execute("UPDATE message_embedding_meta SET content_hash = NULL")
        assert len(pending_messages(conn)) == 2
        assert embed_messages(conn, budget_seconds=0) == 2
        # Re-embedded exactly once: hashes are real now, nothing is pending.
        assert pending_messages(conn) == []
        assert embed_messages(conn, budget_seconds=0) == 0
        hashes = [row[0] for row in conn.execute("SELECT content_hash FROM message_embedding_meta")]
        assert all(
            h == message_vectors._content_hash(t) for h, t in zip(hashes, ["one body", "two body"])
        )
    finally:
        conn.close()


def test_migration_28_is_idempotent_on_rerun(tmp_path, monkeypatch):
    """The content-hash migration is a guarded ALTER; a re-run must be a no-op.

    A legacy store at version 27 gets the column added once, existing rows keep
    NULL hashes (nothing is rewritten or deleted), and applying the full list a
    second time changes nothing.
    """
    # Bring a fresh DB to version 27 only, i.e. the pre-hash schema.
    monkeypatch.setattr(schema_module, "MIGRATIONS", MIGRATIONS[:27])
    conn = initialise(tmp_path / "v2.db")
    conn.execute(
        "INSERT INTO message_embedding_meta(message_key, session_id, seq, model, created)"
        " VALUES ('legacy#m0', 'legacy', 0, 'an-older-model', '2026-01-01T00:00:00')"
    )
    monkeypatch.setattr(schema_module, "MIGRATIONS", MIGRATIONS)

    # First run to 28: column exists, legacy row untouched.
    assert apply_migrations(conn) == 28
    cols = {row[1] for row in conn.execute("PRAGMA table_info(message_embedding_meta)")}
    assert "content_hash" in cols
    legacy = [tuple(row) for row in conn.execute("SELECT content_hash FROM message_embedding_meta")]
    assert legacy == [(None,)]  # no data rewritten or deleted

    # Second run: a no-op, no error, no duplicate version row.
    assert apply_migrations(conn) == 28
    versions = [
        row[0] for row in conn.execute("SELECT version FROM schema_version WHERE version = 28")
    ]
    assert versions == [28]
    conn.close()
