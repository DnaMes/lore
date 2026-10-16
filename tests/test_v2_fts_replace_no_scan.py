"""The v2 writer must not scan the FTS table once per written session.

``search_index.entity_id`` is UNINDEXED, so ``DELETE ... WHERE entity_id = ?``
reads the whole FTS table. With ~6000 sessions that was ~0.3 s per delete, two
deletes per rewritten session — a full ``lore-sync`` ran 28 minutes, read
2.6 TB and hit the 30 min unit timeout.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import open_connection
from lore.storage.writer import StreamingV2Writer, v2_db_path

T0 = datetime(2026, 1, 1)


def _session(sid: str, text: str) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=sid,
        created_at=T0,
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="Title",
        messages=[UnifiedMessage(role=Role.USER, content=text, timestamp=T0)],
    )


def _write(tmp_path: Path, sessions: List[UnifiedSession]) -> List[str]:
    """Stream ``sessions`` through the writer; return every SQL statement it ran."""
    writer = StreamingV2Writer(v2_db_path(tmp_path), {}, {})
    statements: List[str] = []
    writer.conn.set_trace_callback(statements.append)
    writer.begin()
    for session in sessions:
        writer.add_full(session)
    writer.finalize()
    return statements


def _bodies(tmp_path: Path, sid: str) -> List[str]:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        rows = conn.execute(
            "SELECT body FROM search_index WHERE entity_type = 'session' AND entity_id = ?",
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_full_write_never_deletes_fts_rows_by_unindexed_column(tmp_path):
    _write(tmp_path, [_session("a", "first")])
    statements = _write(tmp_path, [_session("a", "second"), _session("b", "other")])

    scans = [s for s in statements if "DELETE FROM search_index" in s and "entity_id" in s]
    assert scans == []


def test_rewrite_replaces_the_single_fts_row(tmp_path):
    _write(tmp_path, [_session("a", "first"), _session("b", "untouched")])
    _write(tmp_path, [_session("a", "second")])

    assert len(_bodies(tmp_path, "a")) == 1
    assert "second" in _bodies(tmp_path, "a")[0]
    assert "first" not in _bodies(tmp_path, "a")[0]
    assert "untouched" in _bodies(tmp_path, "b")[0]


def test_same_session_written_twice_in_one_transaction_keeps_one_row(tmp_path):
    _write(tmp_path, [_session("a", "one"), _session("a", "two")])

    bodies = _bodies(tmp_path, "a")
    assert len(bodies) == 1
    assert "two" in bodies[0]
