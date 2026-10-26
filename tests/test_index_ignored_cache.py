"""v2 index cache must invalidate when ignored.json changes (DB stats unchanged)."""

from __future__ import annotations

import json
import time
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.interfaces import web_data
from lore.storage import reader as storage_reader


def _session(sid: str) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid}",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content=f"body for {sid}",
                timestamp=datetime(2026, 1, 1),
            )
        ],
    )


def test_ignored_json_change_invalidates_v2_index_cache(tmp_path, monkeypatch):
    """Removing an id from ignored.json must re-surface the session without a DB rewrite."""
    IndexBuilder(tmp_path).build_index([_session("keep"), _session("drop")], {})
    ignored = tmp_path / "ignored.json"
    ignored.write_text(json.dumps(["drop"]), encoding="utf-8")

    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    web_data.clear_index_cache()

    calls = {"n": 0}
    real = storage_reader.load_index_v2

    def spy(output_dir):
        calls["n"] += 1
        return real(output_dir)

    monkeypatch.setattr(storage_reader, "load_index_v2", spy)
    # services.index imports load_index_v2 inside the cache fn — patch the
    # storage package export the cache closes over.
    monkeypatch.setattr("lore.storage.load_index_v2", spy)

    first = web_data.load_index()
    assert {s["id"] for s in first["sessions"]} == {"keep"}
    assert calls["n"] == 1

    # Same DB stats; only ignored.json changes (size + mtime).
    time.sleep(0.01)
    ignored.write_text(json.dumps([]), encoding="utf-8")

    restored = web_data.load_index()
    assert {s["id"] for s in restored["sessions"]} == {"keep", "drop"}
    assert restored is not first
    assert calls["n"] == 2

    # Unchanged ignored.json → cache hit, reader not called again.
    assert {s["id"] for s in web_data.load_index()["sessions"]} == {"keep", "drop"}
    assert calls["n"] == 2
