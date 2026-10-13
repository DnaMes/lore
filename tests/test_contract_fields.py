"""Contract fields (origin, relations, message kind/tree, tool events, reasoning), #197."""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import (
    current_version,
    initialise,
    load_index_v2,
    load_session_messages_v2,
    open_connection,
    search_sessions,
    semantic_search_sessions,
)
from lore.storage.embeddings import EMBEDDING_DIM, sqlite_vec_available
from lore.storage.schema import MIGRATIONS, ensure_session_vec_table
from lore.storage.writer import (
    _MAX_SESSION_REASONING_CHARS,
    StreamingV2Writer,
    _replace_session_fts,
    v2_db_path,
    write_sessions,
)

T0 = datetime(2026, 1, 1)


def _msg(role: Role, content: str, **kwargs) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0, **kwargs)


def _session(sid: str, messages: List[UnifiedMessage], **kwargs) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=sid,
        created_at=T0,
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="Contract title",
        messages=messages,
        **kwargs,
    )


def _write(tmp_path: Path, *sessions: UnifiedSession) -> None:
    write_sessions(v2_db_path(tmp_path), list(sessions))


def _body(tmp_path: Path, sid: str) -> Optional[str]:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        row = conn.execute(
            "SELECT body FROM search_index WHERE entity_type = 'session' AND entity_id = ?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_v16_database_migrates_to_17_with_defaults(tmp_path):
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 16]
    try:
        conn = initialise(v2_db_path(tmp_path))
    finally:
        MIGRATIONS[:] = full
    try:
        assert current_version(conn) == 16
        assert "origin" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        conn.execute(
            "INSERT INTO sessions (id, tool, title, created, updated, messages_synced, "
            "messages_count) VALUES ('old', 'codex', 'Old one', 't1', 't2', 1, 1)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, role, content) VALUES ('old', 0, 'user', 'hi')"
        )
    finally:
        conn.close()

    # A reader that has not migrated yet still works and reports the defaults.
    before = load_index_v2(tmp_path)["sessions"][0]
    assert before["origin"] == "unknown" and before["parser_version"] == 0
    old_msgs = load_session_messages_v2(tmp_path, "old")
    assert old_msgs is not None and old_msgs[0].kind == "text"

    conn = initialise(v2_db_path(tmp_path))
    try:
        assert current_version(conn) == max(v for v, _, _ in MIGRATIONS) >= 17
        row = conn.execute(
            "SELECT title, origin, parent_session_id, relation, source_version, parser_version "
            "FROM sessions WHERE id = 'old'"
        ).fetchone()
        assert tuple(row) == ("Old one", "unknown", None, None, None, 0)
        msg = conn.execute(
            "SELECT content, kind, parent_id, active, tool_events_json, reasoning "
            "FROM messages WHERE session_id = 'old'"
        ).fetchone()
        assert tuple(msg) == ("hi", "text", None, 1, None, None)
        origin_idx = {r[1] for r in conn.execute("PRAGMA index_list(sessions)")}
        assert {"idx_sessions_origin", "idx_sessions_parent"} <= origin_idx
    finally:
        conn.close()
    # Re-running is a no-op.
    conn = initialise(v2_db_path(tmp_path))
    conn.close()


def test_roundtrip_preserves_every_contract_field(tmp_path):
    calls = [
        {"tool": "bash", "input": {"cmd": "SECRET"}, "output": "RESULT", "status": "error"},
        {"name": "read", "arguments": {}, "status": "ok", "duration_ms": 12, "is_error": False},
    ]
    session = _session(
        "s1",
        [
            _msg(Role.USER, "start", kind="injected_context", parent_id=None),
            _msg(
                Role.ASSISTANT,
                "answer",
                message_id="m2",
                parent_id="m1",
                active=False,
                tool_calls=calls,
                reasoning="thinking about it",
                kind="compaction_summary",
            ),
        ],
        origin="exec",
        parent_session_id="parent-1",
        relation="subagent",
        source_version="1.2.3",
        parser_version=4,
    )
    _write(tmp_path, session)

    entry = load_index_v2(tmp_path)["sessions"][0]
    assert entry["origin"] == "exec"
    assert entry["parent_session_id"] == "parent-1"
    assert entry["relation"] == "subagent"
    assert entry["source_version"] == "1.2.3"
    assert entry["parser_version"] == 4

    first, second = load_session_messages_v2(tmp_path, "s1", include_reasoning=True) or []
    assert (first.kind, first.parent_id, first.active, first.reasoning) == (
        "injected_context",
        None,
        True,
        None,
    )
    assert first.tool_calls == [] and first.tool_events == []
    assert (second.kind, second.parent_id, second.active) == ("compaction_summary", "m1", False)
    assert second.reasoning == "thinking about it"
    # Compact events get their own field; ``tool_calls`` stays empty so renderers
    # that expect full structured calls never see them.
    assert second.tool_calls == []
    assert second.tool_events == [
        {"name": "bash", "status": "error", "duration_ms": None, "is_error": True},
        {"name": "read", "status": "ok", "duration_ms": 12, "is_error": False},
    ]

    conn = open_connection(v2_db_path(tmp_path))
    try:
        (raw,) = conn.execute("SELECT tool_events_json FROM messages WHERE seq = 1").fetchone()
    finally:
        conn.close()
    assert "SECRET" not in raw and "RESULT" not in raw
    assert json.loads(raw)[0] == {
        "name": "bash",
        "status": "error",
        "duration_ms": None,
        "is_error": True,
    }

    hit = search_sessions(tmp_path, "answer")[0]["session"]
    assert hit["origin"] == "exec" and hit["relation"] == "subagent"


def test_plain_session_keeps_fts_body_and_search(tmp_path):
    _write(
        tmp_path,
        _session("plain", [_msg(Role.USER, "alpha question"), _msg(Role.ASSISTANT, "beta reply")]),
    )
    assert _body(tmp_path, "plain") == "Contract title\nalpha question\nbeta reply"
    assert [h["session"]["id"] for h in search_sessions(tmp_path, "alpha")] == ["plain"]


def test_reasoning_is_not_searchable(tmp_path):
    _write(
        tmp_path,
        _session(
            "r1",
            [_msg(Role.ASSISTANT, "visible text", reasoning="hiddenthought")],
        ),
    )
    assert _body(tmp_path, "r1") == "Contract title\nvisible text"
    assert search_sessions(tmp_path, "hiddenthought") == []


def test_invalid_values_are_normalised(tmp_path):
    _write(
        tmp_path,
        _session(
            "bad",
            [_msg(Role.USER, "x", kind="bogus")],
            origin="robot",
            relation="sibling",
            parser_version="oops",  # type: ignore[arg-type]
        ),
    )
    entry = load_index_v2(tmp_path)["sessions"][0]
    assert (entry["origin"], entry["relation"], entry["parser_version"]) == ("unknown", None, 0)
    (message,) = load_session_messages_v2(tmp_path, "bad") or []
    assert message.kind == "text"


def test_oversized_reasoning_and_events_are_capped(tmp_path):
    calls = [{"tool": "t" * 200, "status": "ok"} for _ in range(80)]
    _write(
        tmp_path,
        _session(
            "big",
            [_msg(Role.ASSISTANT, "x", reasoning="r" * 50_000, tool_calls=calls)],
        ),
    )
    (message,) = load_session_messages_v2(tmp_path, "big", include_reasoning=True) or []
    assert len(message.reasoning or "") == 20_000
    *kept, marker = message.tool_events
    assert len(kept) == 50
    assert all(len(c["name"]) == 80 for c in kept)
    assert marker == {
        "name": "…",
        "status": "truncated",
        "duration_ms": None,
        "is_error": False,
        "dropped": 30,
    }


def _read_tool_events(tmp_path: Path, sid: str):
    messages = load_session_messages_v2(tmp_path, sid) or []
    return [m.tool_events for m in messages]


def _v16_store(tmp_path: Path) -> sqlite3.Connection:
    """Open an un-migrated (v16) store connection."""
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 16]
    try:
        conn = initialise(v2_db_path(tmp_path))
    finally:
        MIGRATIONS[:] = full
    assert current_version(conn) == 16
    return conn


def _v16_session(conn: sqlite3.Connection, sid: str, title: str, body: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, tool, title, created, updated, messages_synced, "
        "messages_count) VALUES (?, 'codex', ?, 't1', 't2', 1, 1)",
        (sid, title),
    )
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content) VALUES (?, 0, 'user', ?)",
        (sid, body),
    )
    _replace_session_fts(conn, sid, "codex", None, title, f"{title}\n{body}")


def test_tool_events_preferred_over_tool_calls(tmp_path):
    events = [{"name": "grep", "status": "ok", "duration_ms": 3, "is_error": False}]
    calls = [{"tool": "bash", "input": {"cmd": "x"}}]
    _write(
        tmp_path, _session("te", [_msg(Role.ASSISTANT, "a", tool_events=events, tool_calls=calls)])
    )
    assert _read_tool_events(tmp_path, "te") == [events]


def test_failed_status_marks_error_without_is_error(tmp_path):
    calls = [{"tool": "bash", "status": "failed"}, {"tool": "ls", "status": "ok"}]
    _write(tmp_path, _session("fl", [_msg(Role.ASSISTANT, "a", tool_calls=calls)]))
    ((first, second),) = _read_tool_events(tmp_path, "fl")
    assert first["is_error"] is True and second["is_error"] is False


@pytest.mark.parametrize("active", [0, False, None])
def test_falsy_active_is_stored_inactive(tmp_path, active):
    _write(tmp_path, _session("act", [_msg(Role.USER, "x", active=active)]))  # type: ignore[arg-type]
    conn = open_connection(v2_db_path(tmp_path))
    try:
        (raw,) = conn.execute("SELECT active FROM messages").fetchone()
    finally:
        conn.close()
    assert raw == 0
    (message,) = load_session_messages_v2(tmp_path, "act") or []
    assert message.active is False


def test_surrogates_and_nul_do_not_break_the_write(tmp_path):
    session = _session(
        "sur",
        [
            _msg(
                Role.ASSISTANT,
                "emoji \ud83d cut",
                reasoning="think \ud83d and nul \x00 end",
                tool_calls=[{"tool": "bash\ud83d", "status": "ok\ud83d"}],
            )
        ],
    )
    _write(tmp_path, session)
    # The next writer must not hit a leaked lock.
    _write(tmp_path, _session("after", [_msg(Role.USER, "fine")]))
    (message,) = load_session_messages_v2(tmp_path, "sur", include_reasoning=True) or []
    assert message.content == "emoji ? cut"
    assert (message.reasoning or "").startswith("think ? and nul ")
    assert message.tool_events[0]["name"] == "bash?"


def test_non_sqlite_failure_aborts_and_releases_the_lock(tmp_path):
    _write(tmp_path, _session("keep", [_msg(Role.USER, "kept")]))
    bad = _session("bad", [_msg(Role.USER, "x")])
    bad.created_at = "not a datetime"  # type: ignore[assignment]
    with pytest.raises(AttributeError):
        _write(tmp_path, bad)
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("BEGIN IMMEDIATE")  # would raise "database is locked" if leaked
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    _write(tmp_path, _session("next", [_msg(Role.USER, "ok")]))
    ids = {s["id"] for s in load_index_v2(tmp_path)["sessions"]}
    assert ids == {"keep", "next"}


def test_reasoning_is_opt_in_in_the_reader(tmp_path):
    _write(tmp_path, _session("rs", [_msg(Role.ASSISTANT, "x", reasoning="secret thoughts")]))
    (default,) = load_session_messages_v2(tmp_path, "rs") or []
    assert default.reasoning is None
    (opted,) = load_session_messages_v2(tmp_path, "rs", include_reasoning=True) or []
    assert opted.reasoning == "secret thoughts"


def test_reader_tolerates_bad_tool_events_json(tmp_path):
    messages = [_msg(Role.ASSISTANT, f"m{i}") for i in range(4)]
    _write(tmp_path, _session("bj", messages))
    conn = open_connection(v2_db_path(tmp_path))
    try:
        for seq, raw in enumerate(
            ['{"not": "a list"}', "{broken", "5", '[1, "x", {"name": "ok"}]']
        ):
            conn.execute(
                "UPDATE messages SET tool_events_json = ? WHERE seq = ?",
                (raw, seq),
            )
    finally:
        conn.close()
    assert _read_tool_events(tmp_path, "bj") == [
        [],
        [],
        [],
        [{"name": "ok", "status": None, "duration_ms": None, "is_error": False}],
    ]


def test_session_reasoning_budget_is_capped(tmp_path):
    messages = [_msg(Role.ASSISTANT, f"m{i}", reasoning="r" * 20_000) for i in range(15)]
    _write(tmp_path, _session("budget", messages))
    loaded = load_session_messages_v2(tmp_path, "budget", include_reasoning=True) or []
    sizes = [len(m.reasoning or "") for m in loaded]
    assert sum(sizes) == _MAX_SESSION_REASONING_CHARS == 200_000
    assert sizes[:10] == [20_000] * 10 and sizes[10:] == [0] * 5
    assert loaded[-1].reasoning is None


def _stored_contract(tmp_path: Path, sid: str):
    conn = open_connection(v2_db_path(tmp_path))
    try:
        row = conn.execute(
            "SELECT origin, parent_session_id, relation, source_version, parser_version, "
            "messages_synced FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    return tuple(row)


def _exec_session(sid: str = "ex") -> UnifiedSession:
    return _session(
        sid,
        [_msg(Role.USER, "first"), _msg(Role.ASSISTANT, "second")],
        origin="exec",
        parent_session_id="par",
        relation="fork",
        source_version="9.9",
        parser_version=3,
    )


_EXEC_CONTRACT = ("exec", "par", "fork", "9.9", 3, 1)


def test_reused_entry_keeps_contract_of_complete_session(tmp_path):
    _write(tmp_path, _exec_session())
    entry = load_index_v2(tmp_path)["sessions"][0]
    entry["title"] = "Renamed"
    write_sessions(v2_db_path(tmp_path), [], reused_entries=[entry])
    assert _stored_contract(tmp_path, "ex") == _EXEC_CONTRACT
    assert [m.content for m in load_session_messages_v2(tmp_path, "ex") or []] == [
        "first",
        "second",
    ]


def test_reused_entry_keeps_contract_of_metadata_only_row(tmp_path):
    _write(tmp_path, _exec_session())
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE sessions SET messages_synced = 0 WHERE id = 'ex'")
    finally:
        conn.close()
    entry = load_index_v2(tmp_path)["sessions"][0]
    write_sessions(v2_db_path(tmp_path), [], reused_entries=[entry])
    assert _stored_contract(tmp_path, "ex")[:5] == _EXEC_CONTRACT[:5]


def _reuse_full(tmp_path: Path, session: UnifiedSession) -> None:
    writer = StreamingV2Writer(v2_db_path(tmp_path), {}, {})
    writer.begin()
    writer.add_reused_full(session)
    writer.finalize()


def test_reused_full_overwrites_contract_with_fresh_extraction(tmp_path):
    """A newer parser version is a re-extraction: the fresh values win."""
    _write(tmp_path, _exec_session())
    fresh = _session("ex", [_msg(Role.USER, "ignored")], origin="cron", parser_version=5)
    _reuse_full(tmp_path, fresh)
    assert _stored_contract(tmp_path, "ex") == ("cron", None, None, None, 5, 1)
    # Messages are reused, not rewritten.
    assert [m.content for m in load_session_messages_v2(tmp_path, "ex") or []] == [
        "first",
        "second",
    ]


def test_reused_full_without_newer_parser_keeps_stored_contract(tmp_path):
    """A reuse that is not a newer parse (B1.1b) never resets the stored contract."""
    _write(tmp_path, _exec_session())
    _reuse_full(tmp_path, _session("ex", [_msg(Role.USER, "ignored")]))
    assert _stored_contract(tmp_path, "ex")[:5] == _EXEC_CONTRACT[:5]


def test_search_falls_back_on_unmigrated_store(tmp_path):
    conn = _v16_store(tmp_path)
    try:
        _v16_session(conn, "old", "Legacy session", "zebrafish content")
    finally:
        conn.close()
    hits = search_sessions(tmp_path, "zebrafish")
    assert [h["session"]["id"] for h in hits] == ["old"]
    assert hits[0]["session"]["origin"] == "unknown"
    assert hits[0]["session"]["parser_version"] == 0


@pytest.mark.skipif(not sqlite_vec_available(), reason="needs sqlite-vec")
def test_semantic_search_falls_back_on_unmigrated_store(tmp_path, monkeypatch):
    conn = _v16_store(tmp_path)
    try:
        _v16_session(conn, "old", "Legacy session", "zebrafish content")
        assert ensure_session_vec_table(conn)
        vec = struct.pack(f"<{EMBEDDING_DIM}f", *([0.25] * EMBEDDING_DIM))
        conn.execute(
            "INSERT INTO session_embeddings(session_id, model, embedding) VALUES (?, ?, ?)",
            ("old", "test", vec),
        )
    finally:
        conn.close()
    monkeypatch.setattr("lore.storage.embeddings.embed_text", lambda text: [0.25] * EMBEDDING_DIM)
    hits = semantic_search_sessions(tmp_path, "anything")
    assert [h["session"]["id"] for h in hits] == ["old"]
    assert hits[0]["session"]["origin"] == "unknown"


def test_half_applied_migration_17_completes_without_data_loss(tmp_path):
    conn = _v16_store(tmp_path)
    try:
        _v16_session(conn, "half", "Half applied", "kept body")
        # An interrupted earlier attempt added some v17 columns but never recorded it.
        conn.execute("ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute("UPDATE sessions SET origin = 'exec' WHERE id = 'half'")
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
    finally:
        conn.close()
    conn = initialise(v2_db_path(tmp_path))
    try:
        assert current_version(conn) == max(v for v, _, _ in MIGRATIONS)
        session_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        message_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert {"origin", "parser_version", "relation"} <= session_cols
        assert {"kind", "active", "reasoning", "tool_events_json"} <= message_cols
        row = conn.execute("SELECT title, origin FROM sessions WHERE id = 'half'").fetchone()
        assert tuple(row) == ("Half applied", "exec")
    finally:
        conn.close()
    (message,) = load_session_messages_v2(tmp_path, "half") or []
    assert message.content == "kept body"


def test_v16_tool_content_roundtrips_unchanged_after_migration(tmp_path):
    body = "ran it\n[Tool: bash]\n{'cmd': 'ls'}\n[/Tool]\ndone"
    conn = _v16_store(tmp_path)
    try:
        _v16_session(conn, "tc", "Tool content", body)
    finally:
        conn.close()
    initialise(v2_db_path(tmp_path)).close()
    (message,) = load_session_messages_v2(tmp_path, "tc") or []
    assert message.content == body
    assert message.tool_calls == [] and message.tool_events == []
