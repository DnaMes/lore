"""User-editable session tags / bookmarks (#56)."""

from __future__ import annotations

from annuli.storage import (
    add_session_tag,
    get_session_tags,
    get_tags_for_sessions,
    list_all_tags,
    remove_session_tag,
)

# ---------------------------------------------------------------------------
# storage layer
# ---------------------------------------------------------------------------


def test_add_and_get_session_tag(tmp_path):
    assert add_session_tag(tmp_path, "s1", "Important") == ["important"]
    assert get_session_tags(tmp_path, "s1") == ["important"]


def test_tags_are_normalized_lowercase_and_stripped(tmp_path):
    add_session_tag(tmp_path, "s1", "  ToReview  ")
    assert get_session_tags(tmp_path, "s1") == ["toreview"]


def test_add_is_idempotent(tmp_path):
    add_session_tag(tmp_path, "s1", "x")
    assert add_session_tag(tmp_path, "s1", "X") == ["x"]  # dup, still one


def test_remove_session_tag(tmp_path):
    add_session_tag(tmp_path, "s1", "a")
    add_session_tag(tmp_path, "s1", "b")
    assert remove_session_tag(tmp_path, "s1", "a") == ["b"]


def test_empty_tag_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        add_session_tag(tmp_path, "s1", "   ")


def test_get_tags_for_sessions_batch(tmp_path):
    add_session_tag(tmp_path, "s1", "a")
    add_session_tag(tmp_path, "s1", "b")
    add_session_tag(tmp_path, "s2", "c")
    result = get_tags_for_sessions(tmp_path, ["s1", "s2", "s3"])
    assert result == {"s1": ["a", "b"], "s2": ["c"]}  # s3 (untagged) omitted


def test_list_all_tags_with_counts(tmp_path):
    add_session_tag(tmp_path, "s1", "shared")
    add_session_tag(tmp_path, "s2", "shared")
    add_session_tag(tmp_path, "s2", "solo")
    assert list_all_tags(tmp_path) == {"shared": 2, "solo": 1}


def test_tags_survive_across_connections(tmp_path):
    """Tags persist in the store, not just in-process (rebuild-safe)."""
    add_session_tag(tmp_path, "s1", "keep")
    # A fresh call opens a new connection to the same db file.
    assert get_session_tags(tmp_path, "s1") == ["keep"]


# ---------------------------------------------------------------------------
# web API
# ---------------------------------------------------------------------------


def _client(monkeypatch, tmp_path):
    from annuli.interfaces import web

    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    # Local-origin guard: tests post from the test client (no Origin header) → allowed.
    return web.app.test_client()


def test_api_session_tags_crud(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = "ses-api-1"

    # GET empty
    assert client.get(f"/api/sessions/{sid}/tags").get_json() == {"tags": []}

    # POST add
    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "Important"})
    assert r.status_code == 200
    assert r.get_json() == {"tags": ["important"]}

    # POST bookmark
    client.post(f"/api/sessions/{sid}/tags", json={"tag": "bookmark"})
    assert client.get(f"/api/sessions/{sid}/tags").get_json() == {"tags": ["bookmark", "important"]}

    # /api/tags aggregate
    assert client.get("/api/tags").get_json() == {"tags": {"bookmark": 1, "important": 1}}

    # DELETE
    r = client.delete(f"/api/sessions/{sid}/tags", json={"tag": "bookmark"})
    assert r.get_json() == {"tags": ["important"]}


def test_api_session_tags_rejects_empty(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.post("/api/sessions/ses-x/tags", json={"tag": ""})
    assert r.status_code == 400


def test_api_session_tags_rejects_invalid_session_id(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.get("/api/sessions/..%2Fetc/tags")
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# MCP get_session surfaces user_tags
# ---------------------------------------------------------------------------


def test_mcp_get_session_includes_user_tags(monkeypatch, tmp_path):
    import asyncio
    import json

    from annuli.interfaces import mcp

    add_session_tag(tmp_path, "session-1", "flagged")

    index = {
        "sessions": [
            {
                "id": "session-1",
                "tool": "claude-code",
                "title": "T",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/demo",
                "thread_id": "thread-1",
                "messages": 1,
                "prompts": 1,
                "export_path": None,
            }
        ],
        "stats": {},
    }
    monkeypatch.setattr(mcp, "load_index", lambda: index)
    monkeypatch.setattr(mcp, "load_sessions_for_tool", lambda _tool=None: [])

    server = mcp.create_server()
    server.output_dir = tmp_path  # point tag lookup at the test store

    resp = asyncio.run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_session", "arguments": {"session_id": "session-1"}},
            }
        )
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["user_tags"] == ["flagged"]
