"""Tests for per-session vector population (#87 PR 2, #93 dedupe, #95 incremental).

``embed_sessions`` incrementally syncs the vec0 ``session_embeddings`` table
(+ its ``session_embedding_meta`` sidecar) to the given sessions, and
``write_sessions`` calls it best-effort after its FTS commit. Both degrade to
a no-op (0 vectors, no crash) when sqlite-vec or fastembed is unavailable.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import session_vectors, v2_db_path, write_sessions
from lore.storage.embeddings import embeddings_available, sqlite_vec_available
from lore.storage.schema import initialise
from lore.storage.session_vectors import embed_sessions

requires_vectors = pytest.mark.skipif(
    not (sqlite_vec_available() and embeddings_available()),
    reason="needs the 'semantic' extra (sqlite-vec + fastembed)",
)


def _session(sid: str, title: str = "Sample", n_messages: int = 2) -> UnifiedSession:
    msgs = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"message body {i} about {title}",
            timestamp=datetime(2026, 1, 1, 12, i),
        )
        for i in range(n_messages)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title=title,
        messages=msgs,
    )


def _vec_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM session_embeddings").fetchone()[0]


def _vec_ids(conn) -> set:
    return {r[0] for r in conn.execute("SELECT session_id FROM session_embeddings")}


# ---------------------------------------------------------------------------
# embed_sessions — direct
# ---------------------------------------------------------------------------


@requires_vectors
def test_embed_sessions_writes_one_vector_each(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    written = embed_sessions(conn, [("a", "hello world", 1), ("b", "goodbye moon", 2)])
    assert written == 2
    assert _vec_count(conn) == 2


@requires_vectors
def test_embed_sessions_skips_empty_text(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    written = embed_sessions(conn, [("a", "real text", 1), ("b", "", 2), ("c", "   ", 3)])
    assert written == 1  # only 'a' had embeddable text
    assert _vec_count(conn) == 1


@requires_vectors
def test_embed_sessions_removes_vanished(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    embed_sessions(conn, [("old-1", "x", 1), ("old-2", "y", 2)])
    embed_sessions(conn, [("new-1", "z", 3)])
    assert _vec_ids(conn) == {"new-1"}


def test_embed_sessions_empty_input_is_zero(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert embed_sessions(conn, []) == 0


@requires_vectors
def test_embed_sessions_deduplicates_ids(tmp_path):
    """Duplicate session ids must not kill the pass (#93 regression).

    vec0 ignores INSERT OR REPLACE and raises 'UNIQUE constraint failed' on a
    duplicate key — real batches contain duplicate ids. Last duplicate wins.
    """
    conn = initialise(tmp_path / "index_v2.sqlite")
    written = embed_sessions(
        conn, [("dup", "first text", 1), ("dup", "second text", 1), ("b", "other", 2)]
    )
    assert written == 2  # dup collapsed to one row, not a crashed pass
    assert _vec_count(conn) == 2


# ---------------------------------------------------------------------------
# Incremental behaviour (#95)
# ---------------------------------------------------------------------------


@requires_vectors
def test_unchanged_sessions_are_not_reembedded(tmp_path, monkeypatch):
    conn = initialise(tmp_path / "index_v2.sqlite")
    embed_sessions(conn, [("a", "some text", 111)])

    calls = {"n": 0}
    real = session_vectors.embed_text

    def counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(session_vectors, "embed_text", counting)
    written = embed_sessions(conn, [("a", "some text", 111)])  # same mtime
    assert written == 0
    assert calls["n"] == 0  # the model was never invoked
    assert _vec_count(conn) == 1  # vector kept


@requires_vectors
def test_changed_mtime_triggers_reembed(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    embed_sessions(conn, [("a", "old content", 111)])
    written = embed_sessions(conn, [("a", "new content", 222)])
    assert written == 1
    assert _vec_count(conn) == 1  # replaced, not duplicated


@requires_vectors
def test_keep_only_items_protected_not_embedded(tmp_path):
    """text=None (reused/metadata-only entries) keep their vector, never embed."""
    conn = initialise(tmp_path / "index_v2.sqlite")
    embed_sessions(conn, [("a", "full text", 1)])
    # Next pass: 'a' is only a reused entry now — vector must survive.
    written = embed_sessions(conn, [("a", None, None)])
    assert written == 0
    assert _vec_ids(conn) == {"a"}


@requires_vectors
def test_budget_zero_stops_before_any_embed(tmp_path):
    """An exhausted budget defers work but keeps the table consistent."""
    conn = initialise(tmp_path / "index_v2.sqlite")
    written = embed_sessions(conn, [("a", "text a", 1), ("b", "text b", 2)], budget_seconds=1e-9)
    assert written == 0  # deadline hit before the first embed
    assert _vec_count(conn) == 0
    # Next pass with headroom picks the deferred sessions up.
    written = embed_sessions(conn, [("a", "text a", 1), ("b", "text b", 2)], budget_seconds=None)
    assert written == 2


def test_env_budget_parsing(monkeypatch):
    monkeypatch.setenv("LORE_EMBED_BUDGET_SECONDS", "0")
    assert session_vectors._embed_budget_seconds() is None  # 0 = unlimited
    monkeypatch.setenv("LORE_EMBED_BUDGET_SECONDS", "45")
    assert session_vectors._embed_budget_seconds() == 45.0
    monkeypatch.setenv("LORE_EMBED_BUDGET_SECONDS", "garbage")
    assert session_vectors._embed_budget_seconds() == 60.0


# ---------------------------------------------------------------------------
# Graceful degradation — no backend
# ---------------------------------------------------------------------------


def test_embed_sessions_noop_without_embeddings(tmp_path, monkeypatch):
    monkeypatch.setattr(session_vectors, "embeddings_available", lambda: False)
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert embed_sessions(conn, [("a", "text", 1)]) == 0


def test_embed_sessions_noop_without_vec_table(tmp_path, monkeypatch):
    monkeypatch.setattr(session_vectors, "embeddings_available", lambda: True)
    monkeypatch.setattr(session_vectors, "ensure_session_vec_table", lambda conn: False)
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert embed_sessions(conn, [("a", "text", 1)]) == 0


# ---------------------------------------------------------------------------
# write_sessions integration — vectors populated after FTS commit
# ---------------------------------------------------------------------------


@requires_vectors
def test_write_sessions_populates_vectors(tmp_path):
    db = v2_db_path(tmp_path)
    count = write_sessions(db, [_session("a"), _session("b", title="Other")])
    assert count == 2
    conn = initialise(db)
    assert _vec_count(conn) == 2


@requires_vectors
def test_write_sessions_vectors_preserve_prior_sessions(tmp_path):
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("old")])
    write_sessions(db, [_session("new")])
    conn = initialise(db)
    assert _vec_ids(conn) == {"new", "old"}


@requires_vectors
def test_write_sessions_reused_entries_keep_vectors(tmp_path):
    """Incremental sync (reused metadata-only rows) must not drop vectors (#95)."""
    db = v2_db_path(tmp_path)
    write_sessions(db, [_session("a", title="Keep Me")])
    reused = [
        {
            "id": "a",
            "tool": "claude-code",
            "project": "/home/u/proj",
            "title": "Keep Me",
            "created": "2026-01-01T00:00:00",
            "updated": "2026-01-02T00:00:00",
            "messages": 2,
            "prompts": 1,
            "search_text": "message body about Keep Me",
        }
    ]
    write_sessions(db, [], reused_entries=reused)
    conn = initialise(db)
    assert _vec_ids(conn) == {"a"}  # previously: full DELETE wiped it


def test_write_sessions_succeeds_without_backend(tmp_path, monkeypatch):
    monkeypatch.setattr("lore.storage.writer.embed_sessions", lambda conn, items: 0)
    db = v2_db_path(tmp_path)
    count = write_sessions(db, [_session("a", title="Findable")])
    assert count == 1
    conn = initialise(db)
    hits = conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'Findable'"
    ).fetchall()
    assert any(r[0] == "a" for r in hits)


def test_write_sessions_survives_embed_exception(tmp_path, monkeypatch):
    def boom(conn, items):
        raise RuntimeError("embed exploded")

    monkeypatch.setattr("lore.storage.writer.embed_sessions", boom)
    db = v2_db_path(tmp_path)
    count = write_sessions(db, [_session("a")])
    assert count == 1  # index still committed
    conn = initialise(db)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
