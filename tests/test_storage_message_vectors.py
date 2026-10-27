"""Per-message vector population (#190).

``embed_messages`` syncs the vec0 ``message_embeddings`` table (+ its
``message_embedding_meta`` sidecar, migration 25) to the store's messages. It is
store-driven and incremental by ``(session_id, seq, model)``, and degrades to a
no-op (0 vectors, no crash) when sqlite-vec or fastembed is unavailable. The
embedder is stubbed in every test, so no model is downloaded.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import message_vectors, v2_db_path, write_sessions
from lore.storage.embeddings import EMBEDDING_DIM, sqlite_vec_available
from lore.storage.message_vectors import embed_messages, message_key, pending_messages
from lore.storage.schema import initialise

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
