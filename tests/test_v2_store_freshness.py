"""The v2 store's generated_at/content hash, its staleness assertion, and the
explicit legacy-JSON fallback (#48, exit criteria 2 and 3).

A stale or internally inconsistent store must never be served silently: the gate
(:func:`v2_is_available`) logs the reason, the service layer logs which store it
served, and :func:`assert_store_fresh` raises when a store cannot be trusted.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.services import index as index_service
from lore.storage import load_index_v2, v2_is_available
from lore.storage.freshness import (
    CONTENT_HASH_KEY,
    GENERATED_AT_KEY,
    StaleStoreError,
    assert_store_fresh,
    store_freshness,
)
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path


def _session(sid: str = "s1", body: str = "searchable body") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid}",
        messages=[
            UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1)),
            UnifiedMessage(role=Role.ASSISTANT, content="reply", timestamp=datetime(2026, 1, 1)),
        ],
    )


def _meta(tmp_path) -> dict:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM store_meta")}
    finally:
        conn.close()


def _set_meta(tmp_path, key: str, value: str) -> None:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO store_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# generated_at + content hash
# ---------------------------------------------------------------------------


def test_index_build_stamps_generated_at_and_content_hash(tmp_path):
    IndexBuilder(tmp_path).build_index([_session()], {})

    meta = _meta(tmp_path)
    assert meta[GENERATED_AT_KEY]
    assert meta[CONTENT_HASH_KEY]

    report = store_freshness(tmp_path)
    assert report["ok"] is True
    assert report["hash_matches"] is True
    assert report["stored_hash"] == report["computed_hash"]


def test_assert_store_fresh_accepts_a_freshly_built_store(tmp_path):
    IndexBuilder(tmp_path).build_index([_session()], {})
    assert assert_store_fresh(tmp_path)["ok"] is True


def test_rows_written_outside_the_build_fail_the_assertion(tmp_path):
    IndexBuilder(tmp_path).build_index([_session()], {})
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE sessions SET messages_count = 99")
        conn.commit()
    finally:
        conn.close()

    report = store_freshness(tmp_path)
    assert report["ok"] is False
    assert any("content hash mismatch" in reason for reason in report["reasons"])

    with pytest.raises(StaleStoreError):
        assert_store_fresh(tmp_path)
    assert v2_is_available(tmp_path) is False


def test_store_stamped_before_the_json_index_is_stale(tmp_path):
    IndexBuilder(tmp_path).build_index([_session()], {})
    index_path = tmp_path / "index.json"
    _set_meta(tmp_path, GENERATED_AT_KEY, "2020-01-01T00:00:00+00:00")

    report = store_freshness(tmp_path, compare_to=index_path)
    assert report["stale_vs_json"] is True
    assert report["ok"] is False
    assert any("before" in reason for reason in report["reasons"])


# ---------------------------------------------------------------------------
# The gate and the service layer say why, never silently
# ---------------------------------------------------------------------------


def test_gate_logs_a_warning_when_it_rejects_a_stale_store(tmp_path, caplog):
    IndexBuilder(tmp_path).build_index([_session()], {})
    index_path = tmp_path / "index.json"
    _set_meta(tmp_path, GENERATED_AT_KEY, "2020-01-01T00:00:00+00:00")
    future = (index_path.stat().st_mtime + 60, index_path.stat().st_mtime + 60)
    os.utime(index_path, future)

    with caplog.at_level(logging.WARNING, logger="lore.storage.reader"):
        assert v2_is_available(tmp_path, compare_to=index_path) is False
    assert any("ignoring the v2 store" in record.message for record in caplog.records)


def test_load_index_logs_the_legacy_fallback_when_v2_is_off(tmp_path, monkeypatch, caplog):
    IndexBuilder(tmp_path).build_index([_session()], {})
    monkeypatch.setenv("LORE_USE_V2", "0")
    index_service.clear_index_cache()

    with caplog.at_level(logging.INFO, logger="lore.services.index"):
        payload = index_service.load_index(tmp_path / "index.json", tmp_path, set())

    assert [s["id"] for s in payload["sessions"]] == ["s1"]
    assert any("serving the legacy JSON index" in r.message for r in caplog.records)


def test_load_index_logs_the_stale_fallback(tmp_path, monkeypatch, caplog):
    IndexBuilder(tmp_path).build_index([_session()], {})
    index_path = tmp_path / "index.json"
    _set_meta(tmp_path, GENERATED_AT_KEY, "2020-01-01T00:00:00+00:00")
    future = (index_path.stat().st_mtime + 60, index_path.stat().st_mtime + 60)
    os.utime(index_path, future)
    monkeypatch.setenv("LORE_USE_V2", "1")
    index_service.clear_index_cache()

    with caplog.at_level(logging.WARNING, logger="lore.services.index"):
        payload = index_service.load_index(index_path, tmp_path, set())

    assert [s["id"] for s in payload["sessions"]] == ["s1"]
    assert any("serving the legacy JSON index" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Search reads the v2 store
# ---------------------------------------------------------------------------


def test_search_reads_the_v2_store_when_it_is_available(tmp_path, monkeypatch):
    IndexBuilder(tmp_path).build_index([_session(body="unique needle phrase")], {})
    # Removing the legacy FTS database proves the hit came from v2.
    (tmp_path / "index.sqlite").unlink()
    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    index_service.clear_index_cache()

    results = index_service.search_index(
        tmp_path / "index.json", "needle", set(), include_automated=True
    )
    assert [r["session"]["id"] for r in results] == ["s1"]


def test_search_logs_the_legacy_fallback(tmp_path, monkeypatch, caplog):
    IndexBuilder(tmp_path).build_index([_session(body="unique needle phrase")], {})
    monkeypatch.setenv("LORE_USE_V2", "0")
    index_service.clear_index_cache()

    with caplog.at_level(logging.INFO, logger="lore.services.index"):
        results = index_service.search_index(
            tmp_path / "index.json", "needle", set(), include_automated=True
        )

    assert [r["session"]["id"] for r in results] == ["s1"]
    assert any("serving the legacy JSON index" in r.message for r in caplog.records)


def test_v2_payload_is_served_by_default(tmp_path, monkeypatch):
    IndexBuilder(tmp_path).build_index([_session()], {})
    monkeypatch.setenv("LORE_USE_V2", "1")
    index_service.clear_index_cache()
    assert load_index_v2(tmp_path)["sessions"][0]["id"] == "s1"
    assert index_service.load_index(tmp_path / "index.json", tmp_path, set())["version"] == "2"
