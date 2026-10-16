"""Tests for utility functions in lore/interfaces/web_data.py."""

from __future__ import annotations

import json

from lore.interfaces import web_data
from lore.interfaces.web_data import (
    _apply_deleted_filter,
    _sanitize_next_url,
    clear_index_cache,
    load_deleted_session_ids,
    remember_deleted_session_id,
)

# ---------------------------------------------------------------------------
# _sanitize_next_url
# ---------------------------------------------------------------------------


def test_sanitize_next_url_empty():
    assert _sanitize_next_url("") == ""


def test_sanitize_next_url_none():
    assert _sanitize_next_url(None) == ""


def test_sanitize_next_url_valid_path():
    assert _sanitize_next_url("/dashboard") == "/dashboard"


def test_sanitize_next_url_valid_with_query():
    assert _sanitize_next_url("/sessions?tool=cursor") == "/sessions?tool=cursor"


def test_sanitize_next_url_rejects_http():
    assert _sanitize_next_url("http://evil.com") == ""


def test_sanitize_next_url_rejects_https():
    assert _sanitize_next_url("https://evil.com/path") == ""


def test_sanitize_next_url_rejects_double_slash():
    assert _sanitize_next_url("//evil.com") == ""


def test_sanitize_next_url_rejects_no_leading_slash():
    assert _sanitize_next_url("relative/path") == ""


def test_sanitize_next_url_whitespace_stripped():
    assert _sanitize_next_url("  /valid/path  ") == "/valid/path"


# ---------------------------------------------------------------------------
# load_deleted_session_ids / remember_deleted_session_id
# ---------------------------------------------------------------------------


def test_load_deleted_session_ids_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", tmp_path / "nonexistent.json")
    result = load_deleted_session_ids()
    assert result == set()


def test_load_deleted_session_ids_dict_format(monkeypatch, tmp_path):
    deleted_file = tmp_path / "deleted.json"
    deleted_file.write_text(json.dumps({"session_ids": ["sess-aaa", "sess-bbb"]}), encoding="utf-8")
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_file)
    # Clear cached version
    web_data._load_deleted_session_ids_cached.cache_clear()

    result = load_deleted_session_ids()
    assert "sess-aaa" in result
    assert "sess-bbb" in result

    web_data._load_deleted_session_ids_cached.cache_clear()


def test_load_deleted_session_ids_list_format(monkeypatch, tmp_path):
    deleted_file = tmp_path / "deleted.json"
    deleted_file.write_text(json.dumps(["sess-xxx", "sess-yyy"]), encoding="utf-8")
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_file)
    web_data._load_deleted_session_ids_cached.cache_clear()

    result = load_deleted_session_ids()
    assert "sess-xxx" in result
    assert "sess-yyy" in result

    web_data._load_deleted_session_ids_cached.cache_clear()


def test_load_deleted_session_ids_exception_returns_empty(monkeypatch, tmp_path):
    deleted_file = tmp_path / "corrupt.json"
    deleted_file.write_text("NOT VALID JSON {{{", encoding="utf-8")
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_file)
    web_data._load_deleted_session_ids_cached.cache_clear()

    result = load_deleted_session_ids()
    assert result == set()

    web_data._load_deleted_session_ids_cached.cache_clear()


def test_remember_deleted_session_id(monkeypatch, tmp_path):
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", tmp_path / "deleted.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    web_data._load_deleted_session_ids_cached.cache_clear()

    remember_deleted_session_id("new-deleted-session")

    result = load_deleted_session_ids()
    assert "new-deleted-session" in result

    web_data._load_deleted_session_ids_cached.cache_clear()


# ---------------------------------------------------------------------------
# _apply_deleted_filter
# ---------------------------------------------------------------------------


def test_apply_deleted_filter_no_deleted(monkeypatch, tmp_path):
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", tmp_path / "nonexistent.json")
    web_data._load_deleted_session_ids_cached.cache_clear()

    payload = {
        "sessions": [
            {"id": "sess-1", "tool": "cursor", "messages": 5, "project": "/proj/a"},
            {"id": "sess-2", "tool": "claude-code", "messages": 3, "project": "/proj/b"},
        ],
        "stats": {},
    }
    result = _apply_deleted_filter(payload)
    assert len(result["sessions"]) == 2


def test_apply_deleted_filter_removes_deleted(monkeypatch, tmp_path):
    deleted_file = tmp_path / "deleted.json"
    deleted_file.write_text(json.dumps({"session_ids": ["sess-bad"]}), encoding="utf-8")
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_file)
    web_data._load_deleted_session_ids_cached.cache_clear()

    payload = {
        "sessions": [
            {"id": "sess-bad", "tool": "cursor", "messages": 5},
            {"id": "sess-good", "tool": "claude-code", "messages": 3},
        ],
        "stats": {},
    }
    result = _apply_deleted_filter(payload)
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["id"] == "sess-good"

    web_data._load_deleted_session_ids_cached.cache_clear()


def test_apply_deleted_filter_updates_stats(monkeypatch, tmp_path):
    deleted_file = tmp_path / "deleted.json"
    deleted_file.write_text(json.dumps({"session_ids": ["sess-del"]}), encoding="utf-8")
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_file)
    web_data._load_deleted_session_ids_cached.cache_clear()

    payload = {
        "sessions": [
            {"id": "sess-del", "tool": "cursor", "messages": 10, "project": "/proj"},
            {"id": "sess-ok", "tool": "cursor", "messages": 5, "project": "/proj"},
        ],
        "stats": {},
    }
    result = _apply_deleted_filter(payload)
    stats = result["stats"]
    assert stats["total_sessions"] == 1
    assert stats["total_messages"] == 5
    assert stats["by_tool"]["cursor"] == 1

    web_data._load_deleted_session_ids_cached.cache_clear()


# ---------------------------------------------------------------------------
# clear_index_cache
# ---------------------------------------------------------------------------


def test_clear_index_cache_does_not_raise():
    # Should be idempotent and not raise
    clear_index_cache()
    clear_index_cache()
