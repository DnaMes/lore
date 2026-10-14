"""Session summary persistence (migration 18) and tombstone cleanup (#197, B4.2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.services.index import remember_deleted_session_id
from lore.storage import current_version, initialise, open_connection
from lore.storage.knowledge import get_session_knowledge, save_session_knowledge
from lore.storage.schema import MIGRATIONS
from lore.storage.summaries import (
    delete_summaries_for_session,
    get_summary,
    has_current_summary,
    list_summaries,
    save_summary,
    summary_fingerprint,
)
from lore.storage.writer import _write_reused_entry, v2_db_path, write_sessions

SID = "ses-sum-1"
PAYLOAD = {"summary": "did things", "cites": []}


def _payload(**extra: object) -> dict:
    return {**PAYLOAD, **extra}


def _save(
    output_dir,
    *,
    level: str = "session",
    scope_key: str | None = None,
    session_id: str = SID,
    **kwargs: object,
) -> None:
    fp = kwargs.pop("fingerprint", summary_fingerprint("text", "fake/model", 1, 1))
    save_summary(
        output_dir,
        level=level,
        scope_key=scope_key or f"session:{session_id}",
        session_id=session_id,
        fingerprint=fp,
        model="fake/model",
        prompt_version=1,
        compressor_version=1,
        payload=kwargs.pop("payload", _payload()),
        **kwargs,
    )


@pytest.fixture
def store(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    initialise(v2_db_path(output_dir))
    return output_dir


def test_migration_18_creates_table_on_fresh_store(store):
    conn = open_connection(v2_db_path(store))
    try:
        assert current_version(conn) == max(v for v, _, _ in MIGRATIONS) >= 18
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'session_summaries'"
        ).fetchone()
    finally:
        conn.close()


def test_migration_18_from_v17(tmp_path):
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 17]
    try:
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        conn = initialise(v2_db_path(output_dir))
    finally:
        MIGRATIONS[:] = full
    conn.close()

    assert get_summary(output_dir, "session", f"session:{SID}") is None

    conn = open_connection(v2_db_path(output_dir))
    try:
        assert current_version(conn) == max(v for v, _, _ in MIGRATIONS) >= 18
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'session_summaries'"
        ).fetchone()
    finally:
        conn.close()


def test_get_save_list_round_trip(store):
    _save(store)
    row = get_summary(store, "session", f"session:{SID}")
    assert row is not None
    assert row["level"] == "session"
    assert row["payload"] == PAYLOAD
    assert row["fingerprint"] == summary_fingerprint("text", "fake/model", 1, 1)

    listed = list_summaries(store)
    assert len(listed) == 1
    assert listed[0]["scope_key"] == f"session:{SID}"


def test_summary_fingerprint_deterministic_and_sensitive():
    a = summary_fingerprint("x", "m", 1, 2)
    b = summary_fingerprint("x", "m", 1, 2)
    assert a == b
    assert a != summary_fingerprint("y", "m", 1, 2)
    assert a != summary_fingerprint("x", "n", 1, 2)
    assert a != summary_fingerprint("x", "m", 2, 2)
    assert a != summary_fingerprint("x", "m", 1, 3)


def test_corrupt_payload_treated_as_absent(store):
    _save(store)
    conn = open_connection(v2_db_path(store))
    try:
        conn.execute(
            "UPDATE session_summaries SET payload_json = '{broken' "
            "WHERE level = 'session' AND scope_key = ?",
            (f"session:{SID}",),
        )
    finally:
        conn.close()
    assert get_summary(store, "session", f"session:{SID}") is None
    assert list_summaries(store) == []


def test_invalid_payload_raises_and_leaves_no_row(store):
    with pytest.raises(ValueError):
        save_summary(
            store,
            level="session",
            scope_key=f"session:{SID}",
            session_id=SID,
            fingerprint="fp",
            model="m",
            prompt_version=1,
            compressor_version=1,
            payload="not a dict",  # type: ignore[arg-type]
        )
    assert list_summaries(store) == []

    huge = {"x": "a" * 70000}
    with pytest.raises(ValueError):
        save_summary(
            store,
            level="session",
            scope_key=f"session:{SID}",
            session_id=SID,
            fingerprint="fp",
            model="m",
            prompt_version=1,
            compressor_version=1,
            payload=huge,
        )
    assert list_summaries(store) == []


def test_save_refused_for_tombstoned_session(store):
    deleted = store / "deleted_sessions.json"
    deleted.write_text(json.dumps({"session_ids": [SID]}), encoding="utf-8")
    _save(store)
    assert list_summaries(store) == []


def test_list_and_get_hide_tombstoned_session_rows(store):
    _save(store)
    other = "ses-other"
    _save(store, session_id=other, scope_key=f"session:{other}")
    (store / "deleted_sessions.json").write_text(
        json.dumps({"session_ids": [SID]}), encoding="utf-8"
    )
    assert get_summary(store, "session", f"session:{SID}") is None
    ids = {r.get("session_id") for r in list_summaries(store)}
    assert SID not in ids
    assert other in ids


def test_aggregate_row_listing_a_tombstoned_session_is_hidden(store):
    _save(
        store, level="day", scope_key="day:2026-01-01", session_id=None, payload={"children": [SID]}
    )
    _save(
        store,
        level="day",
        scope_key="day:2026-01-02",
        session_id=None,
        payload={"children": ["ses-other"]},
    )
    (store / "deleted_sessions.json").write_text(
        json.dumps({"session_ids": [SID]}), encoding="utf-8"
    )
    assert get_summary(store, "day", "day:2026-01-01") is None
    assert get_summary(store, "day", "day:2026-01-02") is not None
    assert [r["scope_key"] for r in list_summaries(store, level="day")] == ["day:2026-01-02"]


def test_save_refuses_an_aggregate_that_lists_a_tombstoned_session(store):
    (store / "deleted_sessions.json").write_text(
        json.dumps({"session_ids": [SID]}), encoding="utf-8"
    )
    _save(
        store, level="day", scope_key="day:2026-01-03", session_id=None, payload={"children": [SID]}
    )
    assert list_summaries(store) == []


def test_session_and_chunk_levels_require_a_matching_session_id(store):
    with pytest.raises(ValueError):
        _save(store, level="session", scope_key="session:x", session_id=None)
    with pytest.raises(ValueError):
        _save(store, level="chunk", scope_key="chunk:other:1-5", session_id=SID)


def test_nan_payload_is_rejected(store):
    with pytest.raises(ValueError):
        _save(store, payload={"one_liner": float("nan")})
    assert list_summaries(store) == []


def test_empty_session_id_filter_does_not_leak_a_connection(store):
    _save(store)
    assert list_summaries(store, session_ids=[]) == []
    assert len(list_summaries(store, session_ids=[SID] * 1200)) >= 1


def test_chunk_rows_are_deleted_by_scope_prefix_too(store):
    _save(store, level="chunk", scope_key=f"chunk:{SID}:1-9")
    assert delete_summaries_for_session(store, SID) >= 1
    assert list_summaries(store) == []


def test_delete_summaries_for_session_counts_and_cleans_day_children(store):
    _save(store, level="session", scope_key=f"session:{SID}")
    _save(store, level="chunk", scope_key=f"chunk:{SID}:0-5", session_id=SID)
    _save(
        store, level="day", scope_key="day:2026-01-01", session_id=None, payload={"children": [SID]}
    )
    _save(
        store,
        level="day",
        scope_key="day:2026-01-02",
        session_id=None,
        payload={"children": ["other"]},
    )

    n = delete_summaries_for_session(store, SID)
    assert n == 3
    remaining = list_summaries(store)
    assert len(remaining) == 1
    assert remaining[0]["scope_key"] == "day:2026-01-02"


def test_has_current_summary(store):
    fp = summary_fingerprint("t", "fake/model", 1, 1)
    _save(store, fingerprint=fp)
    assert has_current_summary(store, SID, fp) is True
    assert has_current_summary(store, SID, "other") is False


def test_get_summary_never_creates_database(tmp_path):
    output_dir = tmp_path / "missing"
    output_dir.mkdir()
    assert not v2_db_path(output_dir).exists()
    assert get_summary(output_dir, "session", f"session:{SID}") is None
    assert not v2_db_path(output_dir).exists()


def test_web_delete_removes_summary_and_knowledge(monkeypatch, store, tmp_path):
    _save(store)
    save_session_knowledge(store, SID, "fp", "m", 1, {"topic": "T"})

    index_path = store / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 1,
                    "total_messages": 1,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [{"id": SID, "tool": "warp", "messages": 1, "export_path": None}],
            }
        ),
        encoding="utf-8",
    )
    deleted_path = store / "deleted_sessions.json"
    monkeypatch.setattr(web_data, "OUTPUT_DIR", store)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        response = client.post(f"/session/{SID}/delete")
    assert response.status_code == 302
    assert get_summary(store, "session", f"session:{SID}") is None
    assert get_session_knowledge(store, SID) is None


def test_remember_deleted_session_id_cleans_summary_and_knowledge(store):
    _save(store)
    save_session_knowledge(store, SID, "fp", "m", 1, {"topic": "T"})
    deleted_path = store / "deleted_sessions.json"

    remember_deleted_session_id(SID, store, deleted_path)

    assert SID in json.loads(deleted_path.read_text())["session_ids"]
    assert get_summary(store, "session", f"session:{SID}") is None
    assert get_session_knowledge(store, SID) is None


def test_resync_after_tombstone_leaves_summary_deleted(store):
    _save(store)
    deleted_path = store / "deleted_sessions.json"
    remember_deleted_session_id(SID, store, deleted_path)

    entry = {
        "id": SID,
        "tool": "codex",
        "title": "t",
        "created": datetime(2026, 1, 1).isoformat(),
        "updated": datetime(2026, 1, 2).isoformat(),
        "search_text": "hello",
        "messages": 2,
    }
    conn = sqlite3.connect(v2_db_path(store))
    try:
        _write_reused_entry(conn, entry)
        conn.commit()
    finally:
        conn.close()

    assert get_summary(store, "session", f"session:{SID}") is None


def test_write_sessions_resync_does_not_resurrect_summary(store):
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=SID,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        messages=[
            UnifiedMessage(role=Role.USER, content="q", timestamp=datetime(2026, 1, 1)),
            UnifiedMessage(role=Role.ASSISTANT, content="a", timestamp=datetime(2026, 1, 1)),
        ],
    )
    write_sessions(v2_db_path(store), [session])
    _save(store)
    remember_deleted_session_id(SID, store, store / "deleted_sessions.json")
    write_sessions(v2_db_path(store), [session])
    assert get_summary(store, "session", f"session:{SID}") is None
