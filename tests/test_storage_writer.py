"""Tests for the v2 dual-write layer (issue #44, PR 2).

`write_sessions` mirrors UnifiedSession objects into the v2 SQLite store.
`IndexBuilder.build_index` calls it as a best-effort dual-write alongside
the legacy index.json / index.sqlite — covered here end to end.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.storage import v2_db_path, write_sessions


def _session(
    sid: str = "s1",
    tool: Tool = Tool.CLAUDE_CODE,
    project: str = "/home/u/proj",
    title: str = "Sample Session",
    n_messages: int = 2,
) -> UnifiedSession:
    msgs = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"message body {i}",
            timestamp=datetime(2026, 1, 1, 12, i),
        )
        for i in range(n_messages)
    ]
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path=project,
        title=title,
        messages=msgs,
    )


# ---------------------------------------------------------------------------
# write_sessions — direct
# ---------------------------------------------------------------------------


def test_write_sessions_returns_count(tmp_path):
    db = tmp_path / "v2.sqlite"
    count = write_sessions(db, [_session("a"), _session("b")])
    assert count == 2


def test_write_sessions_persists_session_row(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("a", title="Hello")])
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id, tool, title FROM sessions").fetchone()
    assert row == ("a", "claude-code", "Hello")


def test_write_sessions_persists_messages_in_order(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("a", n_messages=3)])
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT seq, role, content FROM messages WHERE session_id='a' ORDER BY seq"
    ).fetchall()
    assert [r[0] for r in rows] == [0, 1, 2]
    assert rows[0][1] == "user"
    assert rows[1][1] == "assistant"


def test_write_sessions_preserves_sessions_from_prior_commits(tmp_path):
    """A later write does not erase sessions that were not part of the batch."""
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("old", n_messages=2)])
    write_sessions(db, [_session("new", n_messages=1)])

    conn = sqlite3.connect(db)
    ids = sorted(r[0] for r in conn.execute("SELECT id FROM sessions"))
    assert ids == ["new", "old"]
    assert conn.execute(
        "SELECT content FROM messages WHERE session_id='old' ORDER BY seq"
    ).fetchall() == [("message body 0",), ("message body 1",)]
    assert {
        row[0]
        for row in conn.execute(
            "SELECT entity_id FROM search_index WHERE search_index MATCH 'message'"
        )
    } == {"old", "new"}


def test_write_sessions_replaces_only_a_refreshed_session(tmp_path):
    """Refreshing one id leaves other sessions and their messages intact."""
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("old", n_messages=2), _session("fresh", n_messages=1)])
    write_sessions(db, [_session("old", title="Refreshed", n_messages=3)])

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT title FROM sessions WHERE id='old'").fetchone() == ("Refreshed",)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='old'").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='fresh'").fetchone() == (1,)


def test_reused_entry_does_not_delete_existing_messages(tmp_path):
    """Metadata refreshes preserve complete message rows for an existing id."""
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("kept", n_messages=2)])
    write_sessions(
        db,
        [],
        reused_entries=[
            {
                "id": "kept",
                "tool": "claude-code",
                "project": "/home/u/proj",
                "title": "Kept metadata",
                "created": "2026-01-01",
                "updated": "2026-01-02",
                "messages": 2,
                "search_text": "kept metadata",
            }
        ],
    )

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='kept'").fetchone() == (2,)
    assert conn.execute("SELECT messages_synced FROM sessions WHERE id='kept'").fetchone() == (1,)


def test_write_sessions_title_override(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("a", title="original")], titles={"a": "overridden"})
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT title FROM sessions").fetchone()[0] == "overridden"


def test_search_index_matches_message_body(tmp_path):
    """FTS covers message content, not just titles."""
    db = tmp_path / "v2.sqlite"
    s = _session("a", n_messages=0)
    s.messages = [
        UnifiedMessage(
            role=Role.USER, content="how does kubernetes scale", timestamp=datetime(2026, 1, 1)
        ),
    ]
    write_sessions(db, [s])
    conn = sqlite3.connect(db)
    hits = conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'kubernetes'"
    ).fetchall()
    assert [h[0] for h in hits] == ["a"]


def test_write_sessions_with_reused_entries(tmp_path):
    """Reused dict entries land as metadata-only rows (no messages)."""
    db = tmp_path / "v2.sqlite"
    reused = [
        {
            "id": "old-1",
            "tool": "warp",
            "project": "/p2",
            "thread_id": "t1",
            "title": "Reused Session",
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "search_text": "reused session searchable text",
        }
    ]
    count = write_sessions(db, [_session("new-1")], reused_entries=reused)
    conn = sqlite3.connect(db)
    assert count == 2
    assert sorted(r[0] for r in conn.execute("SELECT id FROM sessions")) == ["new-1", "old-1"]
    # reused entry has no message rows
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='old-1'").fetchone()[0] == 0
    # but is searchable via its search_text
    hits = conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'searchable'"
    ).fetchall()
    assert [h[0] for h in hits] == ["old-1"]


def test_write_sessions_persists_metadata_json(tmp_path):
    db = tmp_path / "v2.sqlite"
    s = _session("a")
    s.summary = "a short summary"
    s.todos = [{"text": "do the thing", "done": False}]
    write_sessions(db, [s])
    conn = sqlite3.connect(db)
    meta = conn.execute("SELECT metadata_json FROM sessions").fetchone()[0]
    assert meta is not None
    assert "short summary" in meta


# ---------------------------------------------------------------------------
# IndexBuilder dual-write — end to end
# ---------------------------------------------------------------------------


def test_index_builder_writes_v2_store(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("e2e", title="E2E")], {})
    v2 = v2_db_path(tmp_path)
    assert v2.exists()
    conn = sqlite3.connect(v2)
    assert conn.execute("SELECT id FROM sessions").fetchone()[0] == "e2e"


def test_index_builder_v2_has_messages(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("e2e", n_messages=4)], {})
    conn = sqlite3.connect(v2_db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_index_builder_legacy_index_still_written(tmp_path):
    """Dual-write must not disturb the legacy index.json / index.sqlite."""
    IndexBuilder(tmp_path).build_index([_session("e2e")], {})
    assert (tmp_path / "index.json").exists()
    assert (tmp_path / "index.sqlite").exists()
    assert v2_db_path(tmp_path).exists()


def test_index_builder_v2_failure_is_non_fatal(tmp_path, monkeypatch):
    """A v2 write error must not break the legacy index path."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated v2 failure")

    # write_sessions_safe swallows; simulate the underlying writer blowing up.
    monkeypatch.setattr("lore.storage.writer.write_sessions", _boom)
    # Must not raise.
    IndexBuilder(tmp_path).build_index([_session("e2e")], {})
    # Legacy index is intact.
    assert (tmp_path / "index.json").exists()


# ---------------------------------------------------------------------------
# #35 — v2 messages complete after incremental sync
# ---------------------------------------------------------------------------


def test_full_session_marked_messages_synced(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a", n_messages=3)], {})
    conn = sqlite3.connect(v2_db_path(tmp_path))
    assert conn.execute("SELECT messages_synced FROM sessions WHERE id='a'").fetchone()[0] == 1


def test_reused_entry_marked_not_messages_synced(tmp_path):
    """A metadata-only reused dict has no message rows -> messages_synced = 0."""
    reused = [
        {
            "id": "old-1",
            "tool": "warp",
            "title": "Reused session realistic title",
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "search_text": "reused content",
        }
    ]
    IndexBuilder(tmp_path).build_index([_session("new-1")], {}, reused_entries=reused)
    conn = sqlite3.connect(v2_db_path(tmp_path))
    rows = dict(conn.execute("SELECT id, messages_synced FROM sessions").fetchall())
    assert rows["new-1"] == 1
    assert rows["old-1"] == 0


def test_reused_sessions_get_full_messages(tmp_path):
    """#35: incremental sync passes full UnifiedSession objects for reused
    sessions -> v2 writes their message rows and marks them synced."""
    new = _session("new-1", n_messages=2)
    reused = _session("reused-1", n_messages=5)
    IndexBuilder(tmp_path).build_index([new], {}, reused_sessions=[reused])
    conn = sqlite3.connect(v2_db_path(tmp_path))
    # both sessions present, both fully synced
    rows = dict(conn.execute("SELECT id, messages_synced FROM sessions").fetchall())
    assert rows == {"new-1": 1, "reused-1": 1}
    # the reused session's 5 message rows were written
    assert (
        conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='reused-1'").fetchone()[0] == 5
    )
