"""Tests for the sqlite-vec session-embedding foundation (#87, PR 1).

sqlite-vec is optional (the 'semantic' extra) and, even when installed, may
fail to load on a Python SQLite build without extension support. These tests
split into:
- behaviour when the extension IS available (real vec0 table + KNN);
- graceful degradation when it is NOT (no table, no crash, FTS-only path).
"""

from __future__ import annotations

import sqlite3
import struct

import pytest

from lore.storage import embeddings
from lore.storage.embeddings import (
    EMBEDDING_DIM,
    load_vec_extension,
    sqlite_vec_available,
)
from lore.storage.schema import ensure_session_vec_table, initialise, open_connection

requires_vec = pytest.mark.skipif(
    not sqlite_vec_available() or not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason="optional 'semantic' extra (sqlite-vec) not installed or sqlite3 lacks enable_load_extension",
)


def _pack(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Availability probe — always runs
# ---------------------------------------------------------------------------


def test_sqlite_vec_available_is_bool():
    # Never raises regardless of whether the package is installed.
    assert isinstance(sqlite_vec_available(), bool)


# ---------------------------------------------------------------------------
# Extension present — real vec0 table + KNN
# ---------------------------------------------------------------------------


@requires_vec
def test_load_vec_extension_succeeds():
    conn = sqlite3.connect(":memory:")
    assert load_vec_extension(conn) is True
    version = conn.execute("select vec_version()").fetchone()[0]
    assert version  # non-empty version string


@requires_vec
def test_ensure_session_vec_table_creates_and_is_idempotent(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert ensure_session_vec_table(conn) is True
    # Second call is a harmless no-op.
    assert ensure_session_vec_table(conn) is True
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "session_embeddings" in tables


@requires_vec
def test_session_vec_table_knn_roundtrip(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert ensure_session_vec_table(conn)

    vectors = {
        "sess-a": [0.0] * EMBEDDING_DIM,
        "sess-b": [1.0] + [0.0] * (EMBEDDING_DIM - 1),
        "sess-c": [0.5] * EMBEDDING_DIM,
    }
    for sid, vec in vectors.items():
        conn.execute(
            "INSERT INTO session_embeddings(session_id, model, embedding) VALUES (?, ?, ?)",
            (sid, embeddings.DEFAULT_MODEL, _pack(vec)),
        )

    query = _pack(vectors["sess-b"])
    rows = conn.execute(
        "SELECT session_id, distance FROM session_embeddings "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        (query,),
    ).fetchall()

    assert len(rows) == 2
    # Nearest neighbour of sess-b's own vector is sess-b (distance ~0).
    assert rows[0][0] == "sess-b"
    assert rows[0][1] == pytest.approx(0.0, abs=1e-5)


@requires_vec
def test_embedding_dim_matches_model():
    """The declared table dim must match what the model actually produces."""
    vec = embeddings.embed_text("hello world")
    if vec is None:  # fastembed absent even though sqlite-vec is present
        pytest.skip("fastembed model unavailable")
    assert len(vec) == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Graceful degradation — extension unavailable
# ---------------------------------------------------------------------------


def test_load_vec_extension_returns_false_when_package_missing(monkeypatch):
    """With the import forced to fail, load returns False and never raises."""
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        pytest.skip("sqlite3 lacks enable_load_extension")

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sqlite_vec":
            raise ImportError("forced missing for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(embeddings, "_vec_failed", False)

    conn = sqlite3.connect(":memory:")
    assert load_vec_extension(conn) is False


def test_ensure_table_returns_false_without_extension(tmp_path, monkeypatch):
    """When the extension can't load, ensure_* is a no-op returning False."""
    monkeypatch.setattr("lore.storage.schema.load_vec_extension", lambda conn: False)
    conn = open_connection(tmp_path / "index_v2.sqlite")
    assert ensure_session_vec_table(conn) is False
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "session_embeddings" not in tables


def test_initialise_succeeds_without_extension(tmp_path, monkeypatch):
    """A DB still initialises cleanly (base schema + migrations) with no vec."""
    monkeypatch.setattr("lore.storage.schema.load_vec_extension", lambda conn: False)
    conn = initialise(tmp_path / "index_v2.sqlite")
    # Base tables from the migration runner are present regardless of vec.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions" in tables
    assert "session_embeddings" not in tables
