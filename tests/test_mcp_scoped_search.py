"""Tests for scoped MCP search (issue #24).

``search_sessions`` / ``search_index`` accept an optional ``scope`` that
limits matches to a message-role subset (user / assistant / tool-result).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.exporters.index import IndexBuilder
from annuli.storage import search_sessions


def _session(sid, *, user="", assistant="", tool_msg="", title="Generic session title"):
    """Build a session with role-tagged messages; empty content is skipped."""
    messages = []
    seq_ts = datetime(2026, 1, 1)
    if user:
        messages.append(UnifiedMessage(role=Role.USER, content=user, timestamp=seq_ts))
    if assistant:
        messages.append(UnifiedMessage(role=Role.ASSISTANT, content=assistant, timestamp=seq_ts))
    if tool_msg:
        messages.append(UnifiedMessage(role=Role.TOOL, content=tool_msg, timestamp=seq_ts))
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=title,
        messages=messages,
    )


def _build(tmp_path):
    """Three sessions, each with the search term in a different role only."""
    IndexBuilder(tmp_path).build_index(
        [
            _session("u", user="please configure xenotoken right now"),
            _session("a", assistant="here is how to configure xenotoken for you"),
            _session("t", tool_msg="grep result line mentioning xenotoken here"),
        ],
        {},
    )


# ---------------------------------------------------------------------------
# search_sessions — scope filtering over the v2 store
# ---------------------------------------------------------------------------


def test_scope_all_returns_every_match(tmp_path):
    _build(tmp_path)
    for scope in (None, "all"):
        hits = search_sessions(tmp_path, "xenotoken", scope=scope)
        assert {h["session"]["id"] for h in hits} == {"u", "a", "t"}


def test_scope_user_only(tmp_path):
    _build(tmp_path)
    hits = search_sessions(tmp_path, "xenotoken", scope="user_only")
    assert {h["session"]["id"] for h in hits} == {"u"}


def test_scope_assistant_only(tmp_path):
    _build(tmp_path)
    hits = search_sessions(tmp_path, "xenotoken", scope="assistant_only")
    assert {h["session"]["id"] for h in hits} == {"a"}


def test_scope_tool_results(tmp_path):
    _build(tmp_path)
    hits = search_sessions(tmp_path, "xenotoken", scope="tool_results")
    assert {h["session"]["id"] for h in hits} == {"t"}


def test_scope_case_insensitive(tmp_path):
    _build(tmp_path)
    hits = search_sessions(tmp_path, "xenotoken", scope="USER_ONLY")
    assert {h["session"]["id"] for h in hits} == {"u"}


def test_scope_multi_term_requires_all_terms_in_role(tmp_path):
    """Both terms must appear in the same role's messages."""
    IndexBuilder(tmp_path).build_index(
        [
            # term split across roles -> no match in either scoped role
            _session("split", user="alpha keyword", assistant="omega keyword"),
            # both terms in the user message
            _session("both", user="alpha and omega keyword together"),
        ],
        {},
    )
    hits = search_sessions(tmp_path, "alpha omega", scope="user_only")
    assert {h["session"]["id"] for h in hits} == {"both"}


def test_scope_no_match_in_role_returns_empty(tmp_path):
    _build(tmp_path)
    # term lives only in the user message -> assistant scope finds nothing
    hits = search_sessions(tmp_path, "xenotoken", scope="assistant_only")
    assert {h["session"]["id"] for h in hits} == {"a"}
    hits = search_sessions(tmp_path, "right now", scope="assistant_only")
    assert hits == []


def test_invalid_scope_raises(tmp_path):
    _build(tmp_path)
    with pytest.raises(ValueError, match="Invalid scope"):
        search_sessions(tmp_path, "xenotoken", scope="bogus")


def test_scope_preserves_result_shape(tmp_path):
    _build(tmp_path)
    hits = search_sessions(tmp_path, "xenotoken", scope="user_only")
    assert len(hits) == 1
    assert "session" in hits[0] and "score" in hits[0]
    assert isinstance(hits[0]["score"], float)


# ---------------------------------------------------------------------------
# web_data.search_index — scope threading + legacy no-op
# ---------------------------------------------------------------------------


def test_web_search_index_scope_v2(tmp_path, monkeypatch):
    from annuli.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "1")
    web_data.clear_index_cache()

    _build(tmp_path)
    hits = web_data.search_index("xenotoken", scope="assistant_only")
    assert {h["session"]["id"] for h in hits} == {"a"}


def test_web_search_index_invalid_scope_raises(tmp_path, monkeypatch):
    from annuli.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "1")
    web_data.clear_index_cache()

    _build(tmp_path)
    with pytest.raises(ValueError, match="Invalid scope"):
        web_data.search_index("xenotoken", scope="nonsense")


def test_web_search_index_legacy_scope_is_noop(tmp_path, monkeypatch):
    """Legacy SearchEngine has no per-message rows: scope is a documented no-op."""
    from annuli.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "0")
    web_data.clear_index_cache()

    _build(tmp_path)
    # scope="user_only" would normally exclude the assistant/tool sessions,
    # but on the legacy path it is ignored — the unfiltered result is returned.
    hits = web_data.search_index("xenotoken", scope="user_only")
    assert {h["session"]["id"] for h in hits} == {"u", "a", "t"}


def test_web_search_index_legacy_rejects_invalid_scope(tmp_path, monkeypatch):
    from annuli.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "0")
    web_data.clear_index_cache()

    _build(tmp_path)
    with pytest.raises(ValueError, match="Invalid scope"):
        web_data.search_index("xenotoken", scope="bogus")


# ---------------------------------------------------------------------------
# MCP search_history handler
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_mcp_search_history_scope(tmp_path, monkeypatch):
    from annuli.interfaces import mcp, web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "1")
    web_data.clear_index_cache()

    _build(tmp_path)
    server = mcp.create_server()
    handler = server.tools["search_history"]["handler"]

    out = json.loads(_run(handler({"query": "xenotoken", "scope": "tool_results"})))
    assert out["scope"] == "tool_results"
    assert {r["id"] for r in out["results"]} == {"t"}


def test_mcp_search_history_default_scope_all(tmp_path, monkeypatch):
    from annuli.interfaces import mcp, web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "1")
    web_data.clear_index_cache()

    _build(tmp_path)
    server = mcp.create_server()
    handler = server.tools["search_history"]["handler"]

    out = json.loads(_run(handler({"query": "xenotoken"})))
    assert out["scope"] == "all"
    assert {r["id"] for r in out["results"]} == {"u", "a", "t"}


def test_mcp_search_history_invalid_scope(tmp_path, monkeypatch):
    from annuli.interfaces import mcp, web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("ANNULI_USE_V2", "1")
    web_data.clear_index_cache()

    _build(tmp_path)
    server = mcp.create_server()
    handler = server.tools["search_history"]["handler"]

    out = _run(handler({"query": "xenotoken", "scope": "garbage"}))
    assert "Invalid scope" in out


def test_mcp_search_history_schema_exposes_scope(tmp_path, monkeypatch):
    from annuli.interfaces import mcp, web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")

    server = mcp.create_server()
    schema = server.tools["search_history"]["inputSchema"]
    scope_prop = schema["properties"]["scope"]
    assert set(scope_prop["enum"]) == {"all", "user_only", "assistant_only", "tool_results"}
