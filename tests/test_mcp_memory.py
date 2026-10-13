"""Tests for the MCP memory_write / memory_recall tools (issue #44 PR 4 / #33).

These tools expose the shared cross-tool memory store over MCP so any
MCP-capable agent can record and recall durable knowledge.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lore.interfaces import mcp


def _server(tmp_path):
    """A fresh MCP server with its output_dir pointed at a temp dir."""
    server = mcp.create_server()
    server.output_dir = tmp_path
    return server


def _call(server, name: str, arguments: dict) -> str:
    handler = server.tools[name]["handler"]
    return asyncio.run(handler(arguments))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_memory_tools_are_registered(tmp_path):
    server = _server(tmp_path)
    names = {t["name"] for t in server.tools.values()}
    assert "memory_write" in names
    assert "memory_recall" in names


# ---------------------------------------------------------------------------
# memory_write
# ---------------------------------------------------------------------------


def test_memory_write_stores_and_returns_id(tmp_path):
    server = _server(tmp_path)
    result = _call(
        server,
        "memory_write",
        {"kind": "fact", "title": "Test fact", "body": "Some content"},
    )
    payload = json.loads(result)
    assert payload["status"] == "stored"
    assert isinstance(payload["memory_id"], int)


def test_memory_write_rejects_unknown_kind(tmp_path):
    server = _server(tmp_path)
    result = _call(server, "memory_write", {"kind": "bogus", "title": "T", "body": "B"})
    assert "Invalid kind" in result


def test_memory_write_requires_title_and_body(tmp_path):
    server = _server(tmp_path)
    assert "required" in _call(server, "memory_write", {"title": "", "body": "B"})
    assert "required" in _call(server, "memory_write", {"title": "T", "body": ""})


def test_memory_write_rejects_bad_tags_type(tmp_path):
    server = _server(tmp_path)
    result = _call(
        server,
        "memory_write",
        {"title": "T", "body": "B", "tags": "not-a-list"},
    )
    assert "list" in result


# ---------------------------------------------------------------------------
# memory_recall
# ---------------------------------------------------------------------------


def test_memory_recall_finds_written_memory(tmp_path):
    server = _server(tmp_path)
    _call(
        server,
        "memory_write",
        {
            "kind": "lesson",
            "title": "Docker layer caching",
            "body": "Order Dockerfile layers least-to-most-changing.",
        },
    )
    result = _call(server, "memory_recall", {"query": "docker caching"})
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["memories"][0]["title"] == "Docker layer caching"


def test_memory_recall_empty_query_lists_all(tmp_path):
    server = _server(tmp_path)
    _call(server, "memory_write", {"title": "One", "body": "first"})
    _call(server, "memory_write", {"title": "Two", "body": "second"})
    payload = json.loads(_call(server, "memory_recall", {}))
    assert payload["count"] == 2


def test_memory_recall_cross_tool_roundtrip(tmp_path):
    """Write as one agent, recall as another — the core vision scenario."""
    server = _server(tmp_path)
    _call(
        server,
        "memory_write",
        {
            "kind": "decision",
            "title": "Use uv for Python",
            "body": "Standardise on uv for venv + installs.",
            "author": "cursor",
        },
    )
    # A separate recall call stands in for an agent in another tool.
    payload = json.loads(_call(server, "memory_recall", {"query": "uv python"}))
    assert payload["count"] == 1
    assert payload["memories"][0]["author"] == "cursor"


def test_memory_recall_invalid_limit(tmp_path):
    server = _server(tmp_path)
    result = _call(server, "memory_recall", {"query": "x", "limit": 9999})
    assert "limit" in result.lower()


# ---------------------------------------------------------------------------
# #197 — hybrid recall tri-state and sources
# ---------------------------------------------------------------------------


SESSION_ID = "0b0d5f5e-7a46-4b0e-9a43-1c2d3e4f5a6b"


def test_memory_recall_returns_sources_and_keeps_shape(tmp_path):
    server = _server(tmp_path)
    _call(
        server,
        "memory_write",
        {"title": "Cache decision", "body": "Use redis", "source_session": SESSION_ID},
    )
    payload = json.loads(_call(server, "memory_recall", {"query": "redis"}))
    assert payload["count"] == 1
    memory = payload["memories"][0]
    assert memory["sources"] == [{"session_id": SESSION_ID, "note": "recorded from this session"}]
    assert memory["source"] == "memory"
    assert "message_id" not in memory["sources"][0]


def test_memory_recall_semantic_tristate(tmp_path, monkeypatch):

    seen = []

    def fake_recall(output_dir, query, **kwargs):
        seen.append(kwargs["semantic"])
        return []

    monkeypatch.setattr("lore.storage.recall_memory", fake_recall)
    server = _server(tmp_path)
    _call(server, "memory_recall", {"query": "x"})
    _call(server, "memory_recall", {"query": "x", "semantic": None})
    _call(server, "memory_recall", {"query": "x", "semantic": False})
    _call(server, "memory_recall", {"query": "x", "semantic": True})
    assert seen == [None, None, False, True]


def test_memory_recall_semantic_false_calls_no_embedder(tmp_path, monkeypatch):
    import lore.storage.embeddings as emb

    server = _server(tmp_path)
    _call(server, "memory_write", {"title": "Cache decision", "body": "Use redis"})
    calls = []
    monkeypatch.setattr(emb, "embed_text", lambda text: calls.append(text))
    monkeypatch.setattr(emb, "embeddings_available", lambda: True)
    payload = json.loads(_call(server, "memory_recall", {"query": "redis", "semantic": False}))
    assert payload["count"] == 1
    assert calls == []


# ---------------------------------------------------------------------------
# #197 — memory_feedback
# ---------------------------------------------------------------------------


def _stored_id(server) -> int:
    result = _call(server, "memory_write", {"title": "Rate me", "body": "feedback target"})
    return json.loads(result)["memory_id"]


def test_memory_feedback_is_registered(tmp_path):
    assert "memory_feedback" in {t["name"] for t in _server(tmp_path).tools.values()}


def test_memory_feedback_records_and_returns_weight(tmp_path):
    server = _server(tmp_path)
    memory_id = _stored_id(server)
    result = _call(server, "memory_feedback", {"memory_id": memory_id, "score": 5})
    payload = json.loads(result)
    # one score of 1.0: (1.0 + 1.5) / (1 + 3)
    assert payload == {"status": "recorded", "weight": 0.625, "count": 1}

    # Same default rater re-rates; a named rater adds a second row.
    _call(server, "memory_feedback", {"memory_id": memory_id, "score": 1})
    again = json.loads(
        _call(server, "memory_feedback", {"memory_id": memory_id, "score": 5, "rater": "cursor"})
    )
    assert again["count"] == 2


@pytest.mark.parametrize("score", [0, 6, -1, "x", 3.5, True, None])
def test_memory_feedback_rejects_invalid_score(tmp_path, score):
    server = _server(tmp_path)
    memory_id = _stored_id(server)
    result = _call(server, "memory_feedback", {"memory_id": memory_id, "score": score})
    assert "score" in result.lower()
    assert "recorded" not in result


@pytest.mark.parametrize("memory_id", [None, "abc", 1.5, False])
def test_memory_feedback_rejects_invalid_memory_id(tmp_path, memory_id):
    result = _call(_server(tmp_path), "memory_feedback", {"memory_id": memory_id, "score": 3})
    assert "memory_id" in result


def test_memory_feedback_unknown_memory(tmp_path):
    result = _call(_server(tmp_path), "memory_feedback", {"memory_id": 4242, "score": 3})
    assert "does not exist" in result


def test_memory_feedback_rejects_bad_rater(tmp_path):
    server = _server(tmp_path)
    memory_id = _stored_id(server)
    result = _call(
        server, "memory_feedback", {"memory_id": memory_id, "score": 3, "rater": "x" * 65}
    )
    assert "Could not record feedback" in result


def test_memory_recall_payload_shows_feedback(tmp_path):
    server = _server(tmp_path)
    memory_id = _stored_id(server)
    before = json.loads(_call(server, "memory_recall", {"query": "feedback"}))
    assert before["memories"][0]["feedback_weight"] == 0.5
    assert before["memories"][0]["feedback_count"] == 0

    _call(server, "memory_feedback", {"memory_id": memory_id, "score": 5})
    after = json.loads(_call(server, "memory_recall", {"query": "feedback"}))
    assert after["memories"][0]["feedback_weight"] == 0.625
    assert after["memories"][0]["feedback_count"] == 1
