"""v2_is_available runs its full check once per change of the store files (A4)."""

from __future__ import annotations

import os
import time

from lore.storage import reader
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path
from tests.test_summarize_pipeline import _build_store, _session


def _counting(monkeypatch):
    calls = []
    real = reader._v2_is_available_uncached

    def _count(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(reader, "_v2_is_available_uncached", _count)
    return calls


def test_unchanged_store_is_checked_once(tmp_path, monkeypatch):
    _build_store(tmp_path, _session("s1", "hello", "world"))
    index_json = tmp_path / "index.json"
    index_json.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(index_json, (old, old))
    calls = _counting(monkeypatch)
    assert reader.v2_is_available(tmp_path, compare_to=index_json)
    assert reader.v2_is_available(tmp_path, compare_to=index_json)
    assert reader.v2_is_available(tmp_path, compare_to=index_json)
    assert len(calls) == 1


def test_newer_index_json_invalidates_the_memo(tmp_path, monkeypatch):
    _build_store(tmp_path, _session("s1", "hello", "world"))
    index_json = tmp_path / "index.json"
    index_json.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(index_json, (old, old))
    calls = _counting(monkeypatch)
    assert reader.v2_is_available(tmp_path, compare_to=index_json)
    future = time.time() + 3600
    os.utime(index_json, (future, future))
    assert not reader.v2_is_available(tmp_path, compare_to=index_json)  # stale now
    assert len(calls) == 2


def test_rows_written_outside_the_build_invalidate_the_memo(tmp_path, monkeypatch):
    _build_store(tmp_path, _session("s1", "hello", "world"))
    calls = _counting(monkeypatch)
    assert reader.v2_is_available(tmp_path)
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE sessions SET messages_count = messages_count + 1 WHERE id = 's1'")
    finally:
        conn.close()
    assert not reader.v2_is_available(tmp_path)
    assert len(calls) == 2
