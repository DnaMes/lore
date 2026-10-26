"""Tests for v2 SQLite search (issue #34).

`search_sessions` queries the v2 store's unified FTS5 index — so search
results and the session list come from the same store and stay consistent.
"""

from __future__ import annotations

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.storage import search_sessions


def _session(sid, tool=Tool.CLAUDE_CODE, title="T", project="/p", body="hello world"):
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path=project,
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def test_search_empty_when_no_store(tmp_path):
    assert search_sessions(tmp_path, "anything") == []


def test_search_empty_query_returns_nothing(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    assert search_sessions(tmp_path, "") == []
    assert search_sessions(tmp_path, "   ") == []


def test_search_matches_title(tmp_path):
    # A natural-length title survives IndexBuilder's low-quality-title filter.
    IndexBuilder(tmp_path).build_index(
        [_session("a", title="Docker compose setup walkthrough")], {}
    )
    hits = search_sessions(tmp_path, "compose")
    assert [h["session"]["id"] for h in hits] == ["a"]


def test_search_matches_message_body(tmp_path):
    """v2 FTS covers message content, not just titles — the key win of #34."""
    IndexBuilder(tmp_path).build_index(
        [_session("a", title="Generic", body="how to configure kubernetes ingress")], {}
    )
    hits = search_sessions(tmp_path, "kubernetes")
    assert [h["session"]["id"] for h in hits] == ["a"]


def test_search_prefix_matching(tmp_path):
    # "kubern" should prefix-match "kubernetes" (FTS5 prefix query).
    IndexBuilder(tmp_path).build_index(
        [_session("a", body="configuring kubernetes ingress rules")], {}
    )
    hits = search_sessions(tmp_path, "kubern")
    assert [h["session"]["id"] for h in hits] == ["a"]


def test_search_result_shape(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a", body="findme distinctive token")], {})
    hits = search_sessions(tmp_path, "findme")
    assert len(hits) == 1
    assert "session" in hits[0] and "score" in hits[0]
    assert isinstance(hits[0]["score"], float)
    assert hits[0]["session"]["id"] == "a"


def test_search_tool_filter(tmp_path):
    IndexBuilder(tmp_path).build_index(
        [
            _session("a", tool=Tool.CLAUDE_CODE, body="shared search term here"),
            _session("b", tool=Tool.CODEX, body="shared search term here"),
        ],
        {},
    )
    hits = search_sessions(tmp_path, "shared", tool="codex")
    assert [h["session"]["id"] for h in hits] == ["b"]


def test_search_project_filter(tmp_path):
    IndexBuilder(tmp_path).build_index(
        [
            _session("a", body="topic discussion content", project="/home/u/alpha"),
            _session("b", body="topic discussion content", project="/home/u/beta"),
        ],
        {},
    )
    hits = search_sessions(tmp_path, "topic", project="alpha")
    assert [h["session"]["id"] for h in hits] == ["a"]


def test_search_respects_limit(tmp_path):
    sessions = [_session(f"s{i}", body="common recurring keyword") for i in range(10)]
    IndexBuilder(tmp_path).build_index(sessions, {})
    hits = search_sessions(tmp_path, "common", limit=3)
    assert len(hits) == 3


def test_search_no_match_returns_empty(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a", body="apples and oranges")], {})
    assert search_sessions(tmp_path, "zzzznonexistent") == []


def test_fts_ignored_ids_excluded_before_limit(tmp_path):
    """Pruned hits must not crowd out a valid result under a tight LIMIT.

    Four stronger pruned matches ranked ahead of one keep → limit=1 still
    returns the keep (ignored ids are filtered in SQL before LIMIT).
    """
    import json

    sessions = [_session(f"drop-{i}", body="token token token token token") for i in range(4)] + [
        _session("keep", body="token")
    ]
    IndexBuilder(tmp_path).build_index(sessions, {})
    (tmp_path / "ignored.json").write_text(
        json.dumps([f"drop-{i}" for i in range(4)]), encoding="utf-8"
    )
    assert [h["session"]["id"] for h in search_sessions(tmp_path, "token", limit=1)] == ["keep"]


def test_fts_many_ignored_ids_use_temp_table_path(tmp_path):
    """More ignored ids than the bound-parameter chunk still exclude correctly."""
    import json

    from lore.storage import search as search_mod

    count = search_mod._IGNORED_SQL_CHUNK + 100
    sessions = [_session(f"drop-{i}", body="token token token") for i in range(count)] + [
        _session("keep", body="token")
    ]
    IndexBuilder(tmp_path).build_index(sessions, {})
    (tmp_path / "ignored.json").write_text(
        json.dumps([f"drop-{i}" for i in range(count)]), encoding="utf-8"
    )
    assert [h["session"]["id"] for h in search_sessions(tmp_path, "token", limit=1)] == ["keep"]


# ---------------------------------------------------------------------------
# web_data.search_index — v2 vs legacy fallback
# ---------------------------------------------------------------------------


def test_web_search_index_uses_v2_when_enabled(tmp_path, monkeypatch):
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    web_data.clear_index_cache()

    IndexBuilder(tmp_path).build_index(
        [_session("v2hit", body="searchable content via v2 store")], {}
    )
    hits = web_data.search_index("searchable")
    assert [h["session"]["id"] for h in hits] == ["v2hit"]


def test_web_search_index_falls_back_to_legacy(tmp_path, monkeypatch):
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    IndexBuilder(tmp_path).build_index(
        [_session("legacyhit", body="searchable content via legacy engine")], {}
    )
    hits = web_data.search_index("searchable")
    assert [h["session"]["id"] for h in hits] == ["legacyhit"]


# ---------------------------------------------------------------------------
# scoped search: the role check follows FTS token and phrase rules (#199)
# ---------------------------------------------------------------------------


def _rich_session(sid, pairs, *, title="Scoped search session"):
    """Session with explicit (role, content) messages."""
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=title,
        messages=[
            UnifiedMessage(role=role, content=content, timestamp=datetime(2026, 1, 1))
            for role, content in pairs
        ],
    )


def _scope_ids(tmp_path, sessions, query, scope):
    IndexBuilder(tmp_path).build_index(sessions, {})
    return {hit["session"]["id"] for hit in search_sessions(tmp_path, query, scope=scope)}


def test_scope_rejects_substring_only_user_message(tmp_path):
    """`cat` must not accept a user message that only contains "concatenate".

    The session is still a candidate (the assistant message says `cat`), so the
    role check is the only thing that can exclude it — a `LIKE '%cat%'` accepted
    it and returned the wrong hit (#199).
    """
    sessions = [
        _rich_session(
            "sub1", [(Role.USER, "concatenate the strings"), (Role.ASSISTANT, "cat fix applied")]
        )
    ]
    assert _scope_ids(tmp_path, sessions, "cat", "user_only") == set()
    assert _scope_ids(tmp_path, sessions, "cat", "assistant_only") == {"sub1"}


def test_scope_accepts_fts_prefix_token(tmp_path):
    """`oauth` matches the token `OAuth2` (FTS prefix rule), not just word-for-word."""
    sessions = [_rich_session("pre1", [(Role.USER, "OAuth2 login fails")])]
    assert _scope_ids(tmp_path, sessions, "oauth", "user_only") == {"pre1"}


def test_scope_applies_porter_stemming(tmp_path):
    """`run` matches a message containing only "running" — the index stems both."""
    sessions = [_rich_session("stem1", [(Role.USER, "running shoes today")])]
    assert _scope_ids(tmp_path, sessions, "run", "user_only") == {"stem1"}


def test_scope_matches_across_diacritics(tmp_path):
    """`cafe` must find "café": the FTS index folds diacritics, the role check must too."""
    sessions = [_rich_session("dia1", [(Role.USER, "café menu today")])]
    assert _scope_ids(tmp_path, sessions, "cafe", "user_only") == {"dia1"}


def test_scope_phrase_needs_consecutive_words(tmp_path):
    sessions = [
        _rich_session("ph1", [(Role.USER, "login then failed")]),
        _rich_session("ph2", [(Role.USER, "login failed")]),
    ]
    assert _scope_ids(tmp_path, sessions, '"login failed"', "user_only") == {"ph2"}


def test_scope_still_filters_by_role(tmp_path):
    sessions = [
        _rich_session("rol1", [(Role.USER, "unrelated notes"), (Role.ASSISTANT, "oauth flow here")])
    ]
    assert _scope_ids(tmp_path, sessions, "oauth", "user_only") == set()
    assert _scope_ids(tmp_path, sessions, "oauth", "assistant_only") == {"rol1"}


def test_scope_tokenizer_matches_the_session_index(tmp_path):
    """The throwaway scope index must use the very tokenizer `search_index` uses."""
    from lore.storage.schema import open_connection
    from lore.storage.search import SCOPE_FTS_TOKENIZER
    from lore.storage.writer import v2_db_path

    IndexBuilder(tmp_path).build_index(
        [_rich_session("tok1", [(Role.USER, "tokenizer drift")])], {}
    )
    conn = open_connection(v2_db_path(tmp_path))
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'search_index'").fetchone()[
            0
        ]
    finally:
        conn.close()
    assert f"tokenize='{SCOPE_FTS_TOKENIZER}'" in sql
