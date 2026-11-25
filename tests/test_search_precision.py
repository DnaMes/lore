"""Keyword hits are never displaced by semantic ones; titles and phrases rank sensibly."""

from __future__ import annotations

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services.index import search_index
from lore.storage import search_sessions, v2_db_path, write_sessions
from lore.storage.fts import build_fts_query


def _session(sid, *, title="T", body="hello world", updated=datetime(2026, 1, 2)):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=updated,
        project_path="/p",
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def _build(tmp_path, sessions):
    write_sessions(v2_db_path(tmp_path), sessions)
    return tmp_path / "index.json"


def _semantic(ids):
    return lambda *a, **k: [
        {"session": {"id": sid}, "score": 0.5 + i / 10} for i, sid in enumerate(ids)
    ]


def test_semantic_only_hits_follow_keyword_hits_and_are_labelled(tmp_path, monkeypatch):
    index_path = _build(
        tmp_path,
        [_session("kw1", body="zebra crossing"), _session("kw2", body="zebra herd")],
    )
    monkeypatch.setattr("lore.storage.semantic_search_sessions", _semantic(["sem1", "kw2"]))
    results = search_index(index_path, "zebra", deleted=set(), limit=10)
    ids = [r["session"]["id"] for r in results]
    assert set(ids[:2]) == {"kw1", "kw2"}
    assert ids[-1] == "sem1" and len(ids) == 3  # kw2 is not repeated as a semantic hit
    labels = {r["session"]["id"]: r["match"] for r in results}
    assert labels == {"kw1": "keyword", "kw2": "keyword", "sem1": "semantic"}
    assert results[-1]["evidence"] is None


def test_vector_pass_is_skipped_when_keyword_hits_fill_the_page(tmp_path, monkeypatch):
    index_path = _build(tmp_path, [_session(f"k{i}", body="zebra") for i in range(3)])
    calls = []
    monkeypatch.setattr(
        "lore.storage.semantic_search_sessions", lambda *a, **k: calls.append(1) or []
    )
    results = search_index(index_path, "zebra", deleted=set(), limit=2)
    assert len(results) == 2 and calls == []
    assert {r["match"] for r in results} == {"keyword"}


def test_pages_tile_with_semantic_fill(tmp_path, monkeypatch):
    index_path = _build(tmp_path, [_session("k1", body="zebra"), _session("k2", body="zebra")])
    monkeypatch.setattr("lore.storage.semantic_search_sessions", _semantic(["s1", "s2"]))
    whole = [r["session"]["id"] for r in search_index(index_path, "zebra", deleted=set(), limit=4)]
    page1 = [r["session"]["id"] for r in search_index(index_path, "zebra", deleted=set(), limit=2)]
    page2 = [
        r["session"]["id"]
        for r in search_index(index_path, "zebra", deleted=set(), limit=2, offset=2)
    ]
    assert page1 + page2 == whole and len(set(whole)) == 4


def test_title_match_outranks_body_match(tmp_path):
    _build(
        tmp_path,
        [
            _session(
                "body", title="Other", body="review gate review gate", updated=datetime(2026, 5, 1)
            ),
            _session(
                "title", title="Review gate design", body="nothing", updated=datetime(2026, 1, 1)
            ),
        ],
    )
    ids = [r["session"]["id"] for r in search_sessions(tmp_path, "review gate")]
    assert ids[0] == "title"


def test_equal_scores_fall_back_to_recency(tmp_path):
    _build(
        tmp_path,
        [
            _session("old", body="zebra", updated=datetime(2026, 1, 1)),
            _session("new", body="zebra", updated=datetime(2026, 6, 1)),
        ],
    )
    ids = [r["session"]["id"] for r in search_sessions(tmp_path, "zebra")]
    assert ids == ["new", "old"]


def test_phrase_inside_a_mixed_query_is_exact(tmp_path):
    _build(
        tmp_path,
        [
            _session("adjacent", body="the review gate passed"),
            _session("apart", body="review the code at the gate"),
        ],
    )
    assert build_fts_query('foo "review gate"') == '"foo"* "review gate"'
    ids = [r["session"]["id"] for r in search_sessions(tmp_path, '"review gate" passed')]
    assert ids == ["adjacent"]


def test_zero_v2_hits_do_not_fall_through_to_the_legacy_engine(tmp_path, monkeypatch):
    index_path = _build(tmp_path, [_session("a", body="alpha")])

    def legacy_must_not_run(*a, **k):
        raise AssertionError("legacy SearchEngine consulted for a no-hit query")

    monkeypatch.setattr("lore.search.engine.SearchEngine", legacy_must_not_run)
    monkeypatch.setattr("lore.storage.semantic_search_sessions", lambda *a, **k: [])
    assert search_index(index_path, "nomatchatall", deleted=set(), limit=5) == []
