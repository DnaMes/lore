"""Tests for semantic search + hybrid RRF fusion (#87, PR 3).

``semantic_search_sessions`` needs sqlite-vec + fastembed to return anything;
those tests skip without the 'semantic' extra. The fusion wiring in
``search_index`` is tested both with the real backend and with the vector pass
stubbed, so keyword-only environments still exercise the fallback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services import index as index_service
from lore.services.index import search_index
from lore.storage import semantic_search_sessions, v2_db_path, write_sessions
from lore.storage.embeddings import embeddings_available, sqlite_vec_available

requires_vectors = pytest.mark.skipif(
    not (sqlite_vec_available() and embeddings_available())
    or not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason="needs the 'semantic' extra (sqlite-vec + fastembed) and sqlite3 with enable_load_extension",
)


def _session(sid, title="T", project="/p", body="hello world"):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path=project,
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def _build(tmp_path, sessions):
    """Write sessions into a v2 store (populates FTS + vectors) and return
    the index.json path search_index expects."""
    write_sessions(v2_db_path(tmp_path), sessions)
    return tmp_path / "index.json"


# ---------------------------------------------------------------------------
# semantic_search_sessions
# ---------------------------------------------------------------------------


def test_semantic_empty_when_no_store(tmp_path):
    assert semantic_search_sessions(tmp_path, "anything") == []


def test_semantic_empty_query(tmp_path):
    assert semantic_search_sessions(tmp_path, "") == []


@requires_vectors
def test_semantic_finds_by_meaning(tmp_path):
    write_sessions(
        v2_db_path(tmp_path),
        [
            _session("k8s", title="Container orchestration", body="deploying pods to a cluster"),
            _session("cook", title="Pasta recipe", body="boil water add salt and noodles"),
        ],
    )
    hits = semantic_search_sessions(tmp_path, "kubernetes deployment", limit=5)
    assert hits, "semantic search returned nothing"
    # The orchestration session should rank above the cooking one for a k8s query.
    ids = [h["session"]["id"] for h in hits]
    assert ids[0] == "k8s"
    assert all("session" in h and "score" in h for h in hits)


@requires_vectors
def test_semantic_tool_filter(tmp_path):
    a = _session("a", body="shared topic")
    b = _session("b", body="shared topic")
    b.tool = Tool.CODEX
    write_sessions(v2_db_path(tmp_path), [a, b])
    hits = semantic_search_sessions(tmp_path, "shared topic", tool="codex")
    assert all(h["session"]["tool"] == "codex" for h in hits)


# ---------------------------------------------------------------------------
# search_index — hybrid fusion wiring
# ---------------------------------------------------------------------------


@requires_vectors
def test_hybrid_surfaces_semantic_only_match(tmp_path):
    """A query that no keyword matches but is semantically close still returns."""
    index_path = _build(
        tmp_path,
        [_session("orch", title="Container orchestration", body="scaling pods and nodes")],
    )
    # 'kubernetes' appears nowhere in the text — FTS alone finds nothing.
    from lore.storage import search_sessions

    assert search_sessions(tmp_path, "kubernetes") == []
    # Hybrid should still surface it via the vector pass.
    results = search_index(index_path, "kubernetes", deleted=set(), limit=5)
    assert any(r["session"]["id"] == "orch" for r in results)


def test_hybrid_disabled_by_env(tmp_path, monkeypatch):
    """LORE_HYBRID_SEARCH=0 keeps search keyword-only (no vector pass)."""
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(index_service, "_hybrid_search", spy)
    index_path = _build(tmp_path, [_session("a", body="findable text")])
    search_index(index_path, "findable", deleted=set(), limit=5)
    assert called["n"] == 0  # hybrid path not taken


def test_hybrid_keeps_fts_when_semantic_empty(tmp_path, monkeypatch):
    """When the vector pass returns nothing, FTS results pass through unchanged."""
    monkeypatch.setattr("lore.storage.semantic_search_sessions", lambda *a, **k: [])
    index_path = _build(tmp_path, [_session("a", body="unique keyword zebra")])
    results = search_index(index_path, "zebra", deleted=set(), limit=5)
    assert any(r["session"]["id"] == "a" for r in results)


def test_hybrid_not_applied_to_scoped_search(tmp_path, monkeypatch):
    """Role-scoped search stays keyword-only (vectors are per-session)."""
    called = {"n": 0}
    monkeypatch.setattr(
        index_service, "_hybrid_search", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    index_path = _build(tmp_path, [_session("a", body="scoped body text")])
    search_index(index_path, "scoped", deleted=set(), limit=5, scope="user_only")
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# distance cutoff (#92)
# ---------------------------------------------------------------------------

_DISTANCE_SQL = (
    "SELECT id, tool, project, thread_id, title, created, updated, messages_count, "
    "prompt_count, total_tokens, prompt_outline, export_path, git_branch, git_commit, "
    "source_path, source_mtime_ns, metadata_json, origin, parent_session_id, relation, "
    "source_version, parser_version, ? AS score FROM sessions WHERE id = ?"
)


def _rows_with_distances(tmp_path, distances):
    """Real ``sessions`` rows carrying a chosen vec distance in ``score``."""
    from lore.storage.schema import open_connection

    conn = open_connection(v2_db_path(tmp_path))
    try:
        return [
            conn.execute(_DISTANCE_SQL, (distance, session_id)).fetchone()
            for session_id, distance in distances.items()
        ]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.88),
        ("", 0.88),
        ("banana", 0.88),
        ("nan", 0.88),
        ("0.5", 0.5),
        ("0", None),  # explicit off switch
        ("-1", None),
    ],
)
def test_semantic_max_distance_env_parsing(monkeypatch, raw, expected):
    from lore.storage.search import semantic_max_distance

    if raw is None:
        monkeypatch.delenv("LORE_SEMANTIC_MAX_DISTANCE", raising=False)
    else:
        monkeypatch.setenv("LORE_SEMANTIC_MAX_DISTANCE", raw)
    assert semantic_max_distance() == expected


def test_semantic_cutoff_drops_far_neighbours(tmp_path):
    from lore.storage.search import _filter_semantic_rows

    _build(tmp_path, [_session("near"), _session("far")])
    rows = _rows_with_distances(tmp_path, {"near": 0.70, "far": 1.10})

    results = _filter_semantic_rows(rows, None, None, 10, False, max_distance=0.88)

    assert [r["session"]["id"] for r in results] == ["near"]


def test_semantic_cutoff_keeps_everything_when_disabled(tmp_path):
    from lore.storage.search import _filter_semantic_rows

    _build(tmp_path, [_session("near"), _session("far")])
    rows = _rows_with_distances(tmp_path, {"near": 0.70, "far": 1.10})

    results = _filter_semantic_rows(rows, None, None, 10, False, max_distance=None)

    assert {r["session"]["id"] for r in results} == {"near", "far"}


def test_semantic_search_sessions_applies_the_configured_cutoff(tmp_path, monkeypatch):
    """The cutoff runs before RRF fusion — the vector pass itself must drop far hits (#92)."""
    from lore.storage.search import _filter_semantic_rows  # noqa: F401 - documents the path

    _build(tmp_path, [_session("near"), _session("far")])
    rows = _rows_with_distances(tmp_path, {"near": 0.70, "far": 1.10})
    monkeypatch.setattr("lore.storage.embeddings.sqlite_vec_available", lambda: True)
    monkeypatch.setattr("lore.storage.embeddings.embed_text", lambda _t: [0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr("lore.storage.search._knn_rows", lambda _conn, _packed, _k: rows)

    hits = semantic_search_sessions(tmp_path, "anything")
    assert [h["session"]["id"] for h in hits] == ["near"]

    monkeypatch.setenv("LORE_SEMANTIC_MAX_DISTANCE", "1.5")
    hits = semantic_search_sessions(tmp_path, "anything")
    assert {h["session"]["id"] for h in hits} == {"near", "far"}
