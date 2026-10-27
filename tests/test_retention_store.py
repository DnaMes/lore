"""Tombstones and pruned ids live in the v2 store and survive a reindex (#48).

Before this, both sets existed only in ``deleted_sessions.json`` and
``ignored.json``. These tests pin that the store is the source of truth: an id
recorded once stays recorded across a rebuild, recording it twice is one row, a
store with no JSON mirror still hides the session, and recording an id never
materialises a store that did not exist.
"""

from __future__ import annotations

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.services.index import (
    load_deleted_session_ids,
    remember_deleted_session_id,
)
from lore.storage import retention
from lore.storage.reader import load_ignored_session_ids
from lore.storage.schema import current_version, open_connection
from lore.storage.writer import v2_db_path


def _session(sid: str, tool: Tool = Tool.CLAUDE_CODE, day: int = 1) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 1, day),
        last_updated=datetime(2026, 1, day, 12),
        project_path="/p",
        title=f"Session {sid}",
        messages=[
            UnifiedMessage(role=Role.USER, content=f"ask {sid}", timestamp=datetime(2026, 1, day)),
            UnifiedMessage(
                role=Role.ASSISTANT, content=f"answer {sid}", timestamp=datetime(2026, 1, day)
            ),
        ],
    )


def _sessions() -> list[UnifiedSession]:
    return [_session("keep", day=2), _session("drop", Tool.CURSOR, day=1)]


def _rows(table: str, tmp_path) -> list[str]:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        return [str(row[0]) for row in conn.execute(f"SELECT session_id FROM {table}")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------


def test_tombstone_is_written_to_the_store_and_survives_a_reindex(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    deleted_path = tmp_path / "deleted_sessions.json"

    remember_deleted_session_id("drop", tmp_path, deleted_path)
    assert _rows("deleted_sessions", tmp_path) == ["drop"]

    # A full rebuild re-extracts every session; the tombstone must stay.
    IndexBuilder(tmp_path).build_index(_sessions(), {})

    assert load_deleted_session_ids(deleted_path) == {"drop"}
    assert _rows("deleted_sessions", tmp_path) == ["drop"]


def test_tombstone_recording_is_idempotent(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    deleted_path = tmp_path / "deleted_sessions.json"

    remember_deleted_session_id("drop", tmp_path, deleted_path)
    remember_deleted_session_id("drop", tmp_path, deleted_path)

    assert _rows("deleted_sessions", tmp_path) == ["drop"]
    assert retention.record_tombstone(tmp_path, "drop") is False  # already stored
    conn = open_connection(v2_db_path(tmp_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM deleted_sessions").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_tombstone_survives_a_missing_json_mirror(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    deleted_path = tmp_path / "deleted_sessions.json"
    remember_deleted_session_id("drop", tmp_path, deleted_path)

    deleted_path.unlink()  # the mirror is optional; the store is the truth

    assert load_deleted_session_ids(deleted_path) == {"drop"}


# ---------------------------------------------------------------------------
# Pruned (ignored) ids
# ---------------------------------------------------------------------------


def test_ignored_ids_are_written_to_the_store_and_survive_a_reindex(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})

    assert retention.record_ignored(tmp_path, ["drop"]) == 1
    assert _rows("ignored_sessions", tmp_path) == ["drop"]

    IndexBuilder(tmp_path).build_index(_sessions(), {})

    assert load_ignored_session_ids(tmp_path) == {"drop"}
    assert _rows("ignored_sessions", tmp_path) == ["drop"]


def test_ignored_ids_survive_a_missing_json_mirror(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    retention.record_ignored(tmp_path, ["drop"])
    (tmp_path / "ignored.json").write_text('["drop"]', encoding="utf-8")

    (tmp_path / "ignored.json").unlink()

    assert load_ignored_session_ids(tmp_path) == {"drop"}


def test_ignored_recording_is_idempotent(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    retention.record_ignored(tmp_path, ["drop", "gone"])
    assert retention.record_ignored(tmp_path, ["drop", "gone"]) == 0
    assert sorted(_rows("ignored_sessions", tmp_path)) == ["drop", "gone"]


def test_strict_loader_keeps_store_ids(tmp_path):
    """A prune rewrite must not drop ids the store holds but the file lost."""
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    retention.record_ignored(tmp_path, ["drop"])
    (tmp_path / "ignored.json").write_text("[]", encoding="utf-8")

    from lore.storage.reader import load_ignored_session_ids_strict

    assert load_ignored_session_ids_strict(tmp_path) == {"drop"}


# ---------------------------------------------------------------------------
# No store: nothing is created
# ---------------------------------------------------------------------------


def test_recording_without_a_store_writes_nothing(tmp_path):
    assert retention.record_tombstone(tmp_path, "drop") is False
    assert retention.record_ignored(tmp_path, ["drop"]) == 0
    assert not v2_db_path(tmp_path).exists()
    assert retention.load_tombstones(tmp_path) == frozenset()
    assert retention.load_ignored(tmp_path) == frozenset()


def test_migration_27_adds_the_ignored_table(tmp_path):
    IndexBuilder(tmp_path).build_index(_sessions(), {})
    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) >= 27
    finally:
        conn.close()
