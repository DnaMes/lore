"""Tests for memoized server-side session filtering and sorting."""

from __future__ import annotations

import os

from lore.interfaces import web


def _index() -> dict:
    return {
        "sessions": [
            {
                "id": "older",
                "tool": "claude-code",
                "created": "2026-01-01T00:00:00",
                "updated": "2026-01-01T00:00:00",
                "keywords": [],
            },
            {
                "id": "newer",
                "tool": "claude-code",
                "created": "2026-01-02T00:00:00",
                "updated": "2026-01-02T00:00:00",
                "keywords": [],
            },
        ]
    }


def test_filtered_sorted_sessions_reuses_cached_ids(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", _index)

    original_filter = web.filter_sessions
    filter_calls = 0

    def tracked_filter(*args, **kwargs):
        nonlocal filter_calls
        filter_calls += 1
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(web, "filter_sessions", tracked_filter)

    first = web._filtered_sorted_sessions("claude-code", "", "", "")
    second = web._filtered_sorted_sessions("claude-code", "", "", "")

    assert [session["id"] for session in first] == ["newer", "older"]
    assert [session["id"] for session in second] == ["newer", "older"]
    assert filter_calls == 1


def test_filtered_sorted_sessions_invalidates_when_index_mtime_changes(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", _index)

    original_filter = web.filter_sessions
    filter_calls = 0

    def tracked_filter(*args, **kwargs):
        nonlocal filter_calls
        filter_calls += 1
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(web, "filter_sessions", tracked_filter)

    web._filtered_sorted_sessions("claude-code", "", "", "")
    mtime_ns = index_path.stat().st_mtime_ns
    os.utime(index_path, ns=(mtime_ns + 1_000_000, mtime_ns + 1_000_000))
    web._filtered_sorted_sessions("claude-code", "", "", "")

    assert filter_calls == 2
