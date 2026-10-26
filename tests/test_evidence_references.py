"""Stable evidence references (#160).

``memory_sources.message_id`` is a SQLite rowid; reindexing a session re-inserts
its message rows, so the integer can silently point at a different message. The
link must therefore carry an identity that survives a rebuild, and the legacy
integer must still resolve.
"""

from __future__ import annotations

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage.evidence import (
    identity_for_message,
    repair_evidence_rowids,
    resolve_evidence,
    unresolved_evidence_links,
)
from lore.storage.memory import add_memory, link_memory_to_session, list_memory_sources
from lore.storage.schema import apply_migrations, current_version, initialise
from lore.storage.writer import write_sessions


def _session(texts=("alpha", "beta", "gamma"), sid: str = "s-evidence") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Evidence",
        messages=[
            UnifiedMessage(role=Role.USER, content=text, timestamp=datetime(2026, 1, 1, i))
            for i, text in enumerate(texts)
        ],
    )


def _archive(tmp_path, session=None):
    output_dir = tmp_path / "state"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_sessions(output_dir / "index_v2.sqlite", [session or _session()])
    return output_dir


def _memory(output_dir, **kwargs):
    return add_memory(
        output_dir,
        kind="fact",
        title="Beta is the second turn",
        body="Derived from the second message.",
        author="test",
        scope_project="/home/u/proj",
        **kwargs,
    )


def _rowids(output_dir) -> dict:
    conn = initialise(output_dir / "index_v2.sqlite")
    try:
        return {
            int(row["seq"]): int(row["id"])
            for row in conn.execute("SELECT id, seq FROM messages ORDER BY seq")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# write side
# ---------------------------------------------------------------------------


def test_link_records_the_stable_identity_next_to_the_legacy_rowid(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    link_memory_to_session(output_dir, memory_id, "s-evidence", message_seq=1, note="second turn")

    (source,) = list_memory_sources(output_dir, memory_id)
    assert source["session_id"] == "s-evidence"
    assert source["message_seq"] == 1
    assert source["message_id"] == _rowids(output_dir)[1]
    assert source["session_namespace"] == "claude-code:local"
    assert len(source["message_sha256"]) == 64
    assert source["resolved"]["message_seq"] == 1
    assert source["resolved_by"] == "rowid"


def test_link_still_accepts_a_legacy_message_rowid(tmp_path):
    output_dir = _archive(tmp_path)
    rowid = _rowids(output_dir)[2]
    memory_id = _memory(output_dir)

    link_memory_to_session(output_dir, memory_id, "s-evidence", message_id=rowid)

    (source,) = list_memory_sources(output_dir, memory_id)
    assert source["message_id"] == rowid
    assert source["message_seq"] == 2
    assert source["resolved"]["message_seq"] == 2


def test_link_survives_a_session_not_yet_synced(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)

    link_memory_to_session(
        output_dir, memory_id, "not-synced-yet", message_seq=4, note="future session"
    )

    (source,) = list_memory_sources(output_dir, memory_id)
    assert source["session_id"] == "not-synced-yet"
    assert source["message_seq"] == 4
    assert source["message_sha256"] is None
    assert source["resolved"] is None


# ---------------------------------------------------------------------------
# reindexing
# ---------------------------------------------------------------------------


def test_evidence_still_points_at_the_same_message_after_a_rebuild(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    link_memory_to_session(output_dir, memory_id, "s-evidence", message_seq=1)
    old_rowid = _rowids(output_dir)[1]

    # Force a real reindex: the writer deletes and re-inserts message rows.
    write_sessions(output_dir / "index_v2.sqlite", [_session()])
    new_rowid = _rowids(output_dir)[1]
    assert new_rowid != old_rowid

    (source,) = list_memory_sources(output_dir, memory_id)
    assert source["resolved"]["message_seq"] == 1
    assert source["resolved"]["message_id"] == new_rowid
    assert source["resolved_by"] == "seq"


def test_repair_repoints_the_legacy_rowid_after_a_rebuild(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    link_memory_to_session(output_dir, memory_id, "s-evidence", message_seq=1)

    write_sessions(output_dir / "index_v2.sqlite", [_session()])
    conn = initialise(output_dir / "index_v2.sqlite")
    try:
        updated = repair_evidence_rowids(conn)
        conn.commit()
        row = conn.execute(
            "SELECT message_id FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        current = conn.execute("SELECT id FROM messages WHERE seq = 1").fetchone()[0]
    finally:
        conn.close()

    assert updated == 1
    assert row["message_id"] == current
    assert list_memory_sources(output_dir, memory_id)[0]["resolved_by"] == "rowid"


def test_repair_never_drops_an_unresolvable_link(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    link_memory_to_session(output_dir, memory_id, "s-evidence", message_seq=1)

    # A rebuild that lost the message (e.g. the provider truncated it). Deleting
    # the row directly keeps the session hash stable, so no divergent copy is
    # created — exactly the "message is gone, link is not" case.
    conn = initialise(output_dir / "index_v2.sqlite")
    conn.execute("DELETE FROM messages WHERE seq = 1")
    conn.commit()
    try:
        before = conn.execute(
            "SELECT message_id FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()[0]
        assert repair_evidence_rowids(conn) == 0
        after = conn.execute(
            "SELECT message_id FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()[0]
        unresolved = unresolved_evidence_links(conn)
    finally:
        conn.close()

    assert before == after
    assert [link["memory_id"] for link in unresolved] == [memory_id]


def test_resolution_falls_back_to_the_content_hash(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    link_memory_to_session(output_dir, memory_id, "s-evidence", message_seq=1)

    # A later provider format renumbers the messages: the sequence and the rowid
    # of the link are both stale, the content is unchanged.
    conn = initialise(output_dir / "index_v2.sqlite")
    try:
        conn.execute("UPDATE messages SET seq = 10 WHERE seq = 1")
        conn.execute(
            "UPDATE memory_sources SET message_seq = NULL, message_id = 999999 WHERE memory_id = ?",
            (memory_id,),
        )
        link = conn.execute(
            "SELECT * FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        resolved = resolve_evidence(conn, link)
    finally:
        conn.close()

    assert resolved is not None
    assert resolved["message_seq"] == 10
    assert resolved["resolved_by"] == "sha256"


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migration_22_is_idempotent_and_keeps_legacy_links(tmp_path):
    output_dir = _archive(tmp_path)
    memory_id = _memory(output_dir)
    conn = initialise(output_dir / "index_v2.sqlite")
    try:
        assert current_version(conn) >= 22
        conn.execute(
            "INSERT INTO memory_sources (memory_id, session_id, message_id, note) "
            "VALUES (?, 's-evidence', NULL, 'legacy')",
            (memory_id,),
        )
        conn.commit()
        apply_migrations(conn)
        row = conn.execute(
            "SELECT message_seq, message_native_id, message_sha256 FROM memory_sources "
            "WHERE note = 'legacy'"
        ).fetchone()
    finally:
        conn.close()

    assert tuple(row) == (None, None, None)
    sources = list_memory_sources(output_dir, memory_id)
    assert any(source["note"] == "legacy" for source in sources)


def test_identity_for_message_reports_the_session_namespace(tmp_path):
    output_dir = _archive(tmp_path)
    conn = initialise(output_dir / "index_v2.sqlite")
    try:
        identity = identity_for_message(conn, "s-evidence", 0)
    finally:
        conn.close()
    assert identity["message_seq"] == 0
    assert identity["session_namespace"] == "claude-code:local"
    assert len(identity["message_sha256"]) == 64
