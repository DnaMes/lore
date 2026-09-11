"""Session/thread meta lookup memoization (#125, #126).

``_index_session_meta`` is called 2-3 times per /session/<id> page load
(session_detail, _enriched_session_for_detail_uncached,
session_messages_fragment), and the MCP server's ``session_meta_by_id`` /
``get_thread`` repeated the same linear scans. These tests pin down the
memoized {id: session} and {thread_id: overview} maps: repeated lookups
must not rescan the session list, and the maps must rebuild whenever the
underlying index payload changes.
"""

from __future__ import annotations

import json

import pytest

from lore.interfaces import web, web_data
from lore.interfaces.web_services import (
    build_threads_overview,
    thread_overview_by_id,
)
from lore.services import index as services_index


def _write_index(index_path, sessions):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"stats": {"total_sessions": len(sessions)}, "sessions": sessions}),
        encoding="utf-8",
    )


def _meta(session_id: str, title: str = "Session", thread_id=None) -> dict:
    return {
        "id": session_id,
        "tool": "warp",
        "title": title,
        "thread_id": thread_id,
        "created": "2026-01-01T00:00:00",
        "updated": "2026-01-01T00:00:00",
        "messages": 1,
        "prompts": 1,
        "project": None,
        "export_path": None,
    }


@pytest.fixture(autouse=True)
def _json_index_only(monkeypatch):
    """Force the legacy JSON reader and reset caches/memos around each test."""
    monkeypatch.setenv("LORE_USE_V2", "0")
    monkeypatch.setattr(services_index, "_SESSION_BY_ID_MEMO", None)
    web_data.clear_index_cache()
    yield
    web_data.clear_index_cache()


def test_index_session_meta_finds_session(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    _write_index(index_path, [_meta("ses-aaa"), _meta("ses-bbb")])
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    web_data.clear_index_cache()

    meta = web._index_session_meta("ses-bbb")
    assert meta is not None
    assert meta["id"] == "ses-bbb"
    assert web._index_session_meta("ses-missing") is None


def test_session_by_id_map_memo_reused_per_payload(monkeypatch):
    """The by-id map is built once per payload object, not per lookup."""
    idx = {"sessions": [_meta("ses-aaa")]}

    monkeypatch.setattr(web, "load_index", lambda: idx)

    first = services_index.session_by_id_map(web.load_index())
    second = services_index.session_by_id_map(web.load_index())
    assert first is second
    assert first["ses-aaa"]["id"] == "ses-aaa"

    # A different payload object (rebuild / tombstone change / test fake)
    # must produce a fresh map instead of serving the stale one.
    new_idx = {"sessions": [_meta("ses-zzz")]}
    monkeypatch.setattr(web, "load_index", lambda: new_idx)
    rebuilt = services_index.session_by_id_map(web.load_index())
    assert rebuilt is not first
    assert rebuilt["ses-zzz"]["id"] == "ses-zzz"
    assert "ses-aaa" not in rebuilt


def test_session_by_id_map_first_occurrence_wins():
    """Duplicate ids resolve to the first entry, matching the old next() scan."""
    idx = {"sessions": [_meta("ses-dup", title="First"), _meta("ses-dup", title="Second")]}
    assert services_index.session_by_id_map(idx)["ses-dup"]["title"] == "First"


def test_index_session_meta_respects_tombstones(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    _write_index(index_path, [_meta("ses-keep"), _meta("ses-gone")])
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: {"ses-gone"})
    web_data.clear_index_cache()

    assert web._index_session_meta("ses-keep") is not None
    assert web._index_session_meta("ses-gone") is None


def test_load_index_repeats_are_memoized(monkeypatch, tmp_path):
    """Repeated load_index() calls return the same finalized payload object."""
    index_path = tmp_path / "index.json"
    _write_index(index_path, [_meta("ses-aaa", title="Alpha")])
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    web_data.clear_index_cache()

    first = web_data.load_index()
    second = web_data.load_index()
    assert first is second
    # Finalization (display titles) is applied exactly once-visible.
    assert first["sessions"][0]["display_title"] == "Alpha"


def test_load_index_tombstone_change_without_cache_clear(monkeypatch, tmp_path):
    """Un-deleting must re-expose the session even with an unchanged stat key.

    The finalized payload is cached per (stat, tombstone set), and the raw
    cached payload is never contaminated by a filtered result — so a caller
    that shrinks the tombstone set sees sessions again without waiting for
    an index rewrite.
    """
    index_path = tmp_path / "index.json"
    _write_index(index_path, [_meta("ses-keep"), _meta("ses-tomb")])
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: {"ses-tomb"})
    web_data.clear_index_cache()

    filtered = web_data.load_index()
    assert [s["id"] for s in filtered["sessions"]] == ["ses-keep"]

    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    restored = web_data.load_index()
    assert sorted(s["id"] for s in restored["sessions"]) == ["ses-keep", "ses-tomb"]
    assert restored is not filtered


def test_load_index_invalidates_on_file_change(monkeypatch, tmp_path):
    """A rewritten index.json (new stat key) is picked up without cache_clear."""
    index_path = tmp_path / "index.json"
    _write_index(index_path, [_meta("ses-old")])
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    web_data.clear_index_cache()

    assert web_data.load_index()["sessions"][0]["id"] == "ses-old"

    # Different content → different size → different stat cache key even on
    # coarse-mtime filesystems.
    _write_index(index_path, [_meta("ses-new-who-dis")])
    assert web_data.load_index()["sessions"][0]["id"] == "ses-new-who-dis"
    assert web._index_session_meta("ses-new-who-dis") is not None


def test_thread_overview_by_id_memo_reused_per_list():
    """The {thread_id: overview} map is built once per sessions list (#126)."""
    sessions = [
        _meta("ses-a", title="A1", thread_id="thr-1"),
        _meta("ses-b", title="B1", thread_id="thr-1"),
        _meta("ses-c", title="C1", thread_id="thr-2"),
    ]

    first = thread_overview_by_id(sessions)
    second = thread_overview_by_id(sessions)
    assert first is second
    assert first["thr-1"]["count"] == 2
    assert first["thr-2"]["count"] == 1

    # A different list object rebuilds instead of serving the stale map.
    new_sessions = [_meta("ses-d", title="D1", thread_id="thr-3")]
    rebuilt = thread_overview_by_id(new_sessions)
    assert rebuilt is not first
    assert set(rebuilt) == {"thr-3"}


def test_thread_overview_by_id_matches_build_threads_overview():
    """The memoized buckets agree with the canonical overview builder."""
    sessions = [
        _meta(
            "ses-a",
            title="Old",
            thread_id="thr-1",
        ),
        _meta("ses-b", title="New", thread_id="thr-1"),
    ]
    sessions[0]["updated"] = "2026-01-01T00:00:00"
    sessions[1]["updated"] = "2026-02-01T00:00:00"

    overview_list = {t["thread_id"]: t for t in build_threads_overview(sessions)}
    by_id = thread_overview_by_id(sessions)
    assert overview_list == by_id
    # Newest session wins title/project/updated for the thread.
    assert by_id["thr-1"]["title"] == "New"
    assert by_id["thr-1"]["updated"] == "2026-02-01T00:00:00"


def test_mcp_session_and_thread_lookups_via_shared_memo(monkeypatch):
    """MCP get_session/get_thread use the memoized lookups end to end (#126)."""
    import asyncio
    import json as json_module

    from lore.interfaces import mcp

    idx = {
        "sessions": [_meta("ses-mcp-0001", title="Memo", thread_id="thr-mcp-0001")],
        "stats": {"total_sessions": 1},
    }
    monkeypatch.setattr(mcp, "load_index", lambda: idx)
    monkeypatch.setattr(mcp, "load_sessions_for_tool", lambda _tool=None: [])

    server = mcp.create_server()

    def call_tool(name, arguments):
        response = asyncio.run(
            server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        )
        assert response is not None
        return json_module.loads(response["result"]["content"][0]["text"])

    # Repeated calls hit the memoized maps and keep returning the same data.
    for _ in range(2):
        session_payload = call_tool("get_session", {"session_id": "ses-mcp-0001"})
        assert session_payload["id"] == "ses-mcp-0001"
        assert session_payload["live"] is False

        thread_payload = call_tool("get_thread", {"thread_id": "thr-mcp-0001"})
        assert thread_payload["thread"]["id"] == "thr-mcp-0001"

    assert call_tool("get_session", {"session_id": "ses-missing-1"}) == {
        "error": "Session not found",
        "session_id": "ses-missing-1",
    }
    assert call_tool("get_thread", {"thread_id": "thr-missing-1"}) == {
        "error": "Thread not found",
        "thread_id": "thr-missing-1",
    }
