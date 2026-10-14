"""Tests for OpenCode SQLite v2 tables (session_v2, session_message)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lore.core.models import Role, Tool
from lore.extractors.opencode import OpenCodeExtractor

_SENTINEL = "SENTINEL_MUST_NOT_LEAK_7f3a"
_FAKE_HOME = "/tmp/opencode/lore-test-home"
_PROJECT = "/tmp/fake-project/repo"


def _session_v2_ddl() -> str:
    return """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            workspace_id TEXT,
            parent_id TEXT,
            fork_session_id TEXT,
            slug TEXT,
            directory TEXT,
            path TEXT,
            title TEXT,
            version TEXT,
            share_url TEXT,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files INTEGER,
            metadata TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            agent TEXT,
            model TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            time_idle INTEGER,
            time_viewed INTEGER,
            idle_outcome TEXT,
            time_compacting INTEGER,
            time_archived INTEGER
        )
    """


def _session_message_ddl() -> str:
    return """
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            seq INTEGER NOT NULL,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT NOT NULL
        )
    """


def _create_v2_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(_session_v2_ddl())
    conn.execute(_session_message_ddl())
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    directory: str = _PROJECT,
    title: str = "V2 Session",
    version: str = "1.0.0",
    parent_id: str | None = None,
    time_created: int = 1_700_000_000_000,
    time_updated: int = 1_700_000_060_000,
) -> None:
    conn.execute(
        """
        INSERT INTO session_v2 (
            id, project_id, directory, title, version, parent_id,
            time_created, time_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, "proj-v2", directory, title, version, parent_id, time_created, time_updated),
    )


def _insert_message(
    conn: sqlite3.Connection,
    session_id: str,
    msg_id: str,
    msg_type: str,
    seq: int,
    data: dict,
    time_created: int = 1_700_000_000_000,
) -> None:
    conn.execute(
        """
        INSERT INTO session_message (
            id, session_id, type, seq, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (msg_id, session_id, msg_type, seq, time_created, time_created, json.dumps(data)),
    )


def _extract(db_path: Path, monkeypatch, tmp_path: Path) -> list:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(db_path))
    extractor = OpenCodeExtractor(force_full=True)
    return list(extractor.extract_sessions())


def test_parser_version_bumped():
    assert OpenCodeExtractor.PARSER_VERSION == 2


def test_v2_user_assistant_tools_ordered_by_seq(monkeypatch, tmp_path):
    db = tmp_path / "v2.db"
    conn = _create_v2_db(db)
    sid = "ses_v2_main"
    _insert_session(conn, sid)
    _insert_message(conn, sid, "msg_u1", "user", 1, {"text": "hello user"})
    _insert_message(conn, sid, "msg_u2", "user", 2, {"text": "second"})
    _insert_message(conn, sid, "msg_u3", "user", 3, {"text": "third"})
    _insert_message(
        conn,
        sid,
        "msg_syn",
        "synthetic",
        4,
        {"text": "injected syn", "description": "ctx"},
    )
    _insert_message(conn, sid, "msg_sys", "system", 5, {"text": "system ctx"})
    _insert_message(
        conn, sid, "msg_skill", "skill", 6, {"skill": "x", "name": "n", "text": "skill ctx"}
    )
    assistant_data = {
        "agent": "build",
        "model": {"id": "gpt-test", "providerID": "openai"},
        "content": [
            {"type": "text", "text": "answer line"},
            {"type": "reasoning", "text": "think hard"},
            {
                "type": "tool",
                "id": "tc1",
                "name": "read_file",
                "time": {"created": 1, "ran": 100, "completed": 250},
                "state": {
                    "status": "completed",
                    "input": {"path": "/tmp/x"},
                    "content": [{"type": "text", "text": "file body 42"}],
                },
            },
            {
                "type": "tool",
                "id": "tc2",
                "name": "broken",
                "state": {"status": "error", "input": {}},
            },
            {
                "type": "tool",
                "id": "tc3",
                "name": "wait",
                "state": {"status": "running", "input": {}},
            },
        ],
        "tokens": {
            "input": 10,
            "output": 20,
            "reasoning": 3,
            "cache": {"read": 1, "write": 2},
        },
        "time": {"created": 1_700_000_001_000},
    }
    _insert_message(conn, sid, "msg_a", "assistant", 7, assistant_data)
    _insert_message(
        conn,
        sid,
        "msg_compact_ok",
        "compaction",
        8,
        {"status": "completed", "summary": "compacted story", "reason": "long"},
    )
    _insert_message(
        conn,
        sid,
        "msg_compact_run",
        "compaction",
        9,
        {"status": "running", "summary": "nope"},
    )
    _insert_message(
        conn,
        sid,
        "msg_shell",
        "shell",
        10,
        {"command": "echo hi", "status": "done", "output": "hi\n"},
    )
    _insert_message(conn, sid, "msg_idle", "idle", 11, {})
    _insert_message(conn, sid, "msg_fake", "totally-unknown-type", 12, {})
    conn.commit()
    conn.close()

    sessions = _extract(db, monkeypatch, tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == sid
    assert s.tool == Tool.OPENCODE
    assert s.origin == "interactive"
    user_msgs = [m for m in s.messages if m.role == Role.USER]
    assert len(user_msgs) == 3
    injected = [m for m in s.messages if m.kind == "injected_context"]
    assert len(injected) == 3
    assistant = next(m for m in s.messages if m.role == Role.ASSISTANT)
    assert assistant.model == "openai/gpt-test"
    assert assistant.tokens == {
        "input": 10,
        "output": 20,
        "reasoning": 3,
        "cache_read": 1,
        "cache_write": 2,
    }
    assert assistant.reasoning == "think hard"
    assert "answer line" in assistant.content
    assert "[Tool: read_file]" in assistant.content
    assert len(assistant.tool_events) == 3
    assert assistant.tool_events[0]["status"] == "completed"
    assert assistant.tool_events[0]["duration_ms"] == 150
    assert assistant.tool_events[1]["is_error"] is True
    assert assistant.tool_events[2]["status"] == "running"
    compaction = [m for m in s.messages if m.kind == "compaction_summary"]
    assert len(compaction) == 1
    assert compaction[0].content == "compacted story"
    tool_msgs = [m for m in s.messages if m.role == Role.TOOL]
    assert any(m.content == "file body 42" for m in tool_msgs)
    shell = next(m for m in tool_msgs if m.content.startswith("[Shell:"))
    assert shell.content.startswith("[Shell: echo hi]")
    assert "hi" in shell.content
    texts = [m.content for m in s.messages]
    assert "running" not in " ".join(texts) or "[Tool: wait]" in assistant.content


def test_subagent_parent_id(monkeypatch, tmp_path):
    db = tmp_path / "sub.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_parent", parent_id=None)
    _insert_message(conn, "ses_parent", "m1", "user", 1, {"text": "p"})
    _insert_session(conn, "ses_child", parent_id="ses_parent")
    for i in range(3):
        _insert_message(conn, "ses_child", f"u{i}", "user", i + 1, {"text": f"u{i}"})
    conn.commit()
    conn.close()

    sessions = {s.session_id: s for s in _extract(db, monkeypatch, tmp_path)}
    child = sessions["ses_child"]
    assert child.origin == "subagent"
    assert child.relation == "subagent"
    assert child.parent_session_id == "ses_parent"


def test_v1_session_id_wins_over_v2(monkeypatch, tmp_path):
    db = tmp_path / "both.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            time_created INTEGER,
            time_updated INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            message_id TEXT,
            time_created INTEGER,
            data TEXT
        )
        """
    )
    conn.execute(_session_v2_ddl())
    conn.execute(_session_message_ddl())
    shared = "ses_shared_id"
    ts = 1_700_000_000_000
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        (shared, "p", _PROJECT, "V1 title", "0.1", ts, ts + 1),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("msg-v1", shared, ts, json.dumps({"role": "user", "time": {"created": ts}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        (
            "prt-v1",
            shared,
            "msg-v1",
            ts,
            json.dumps({"type": "text", "text": "v1-winner-text"}),
        ),
    )
    _insert_session(conn, shared, title="V2 title")
    _insert_message(conn, shared, "msg-v2", "user", 1, {"text": "v2-loser-text"})
    conn.commit()
    conn.close()

    sessions = _extract(db, monkeypatch, tmp_path)
    assert len(sessions) == 1
    assert sessions[0].title == "V1 title"
    assert sessions[0].messages[0].content == "v1-winner-text"


def test_v2_without_session_message_table_skipped(monkeypatch, tmp_path):
    db = tmp_path / "no_msg.db"
    conn = sqlite3.connect(db)
    conn.execute(_session_v2_ddl())
    _insert_session(conn, "ses_lonely")
    conn.commit()
    conn.close()
    assert _extract(db, monkeypatch, tmp_path) == []


def test_corrupt_db_skipped_without_raising(monkeypatch, tmp_path):
    db = tmp_path / "bad.db"
    db.write_bytes(b"not a sqlite file at all" * 100)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(db))
    extractor = OpenCodeExtractor(force_full=True)
    list(extractor.extract_sessions())


def test_original_db_unchanged_no_sidecars(monkeypatch, tmp_path):
    db = tmp_path / "pristine.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_p")
    _insert_message(conn, "ses_p", "m1", "user", 1, {"text": "a"})
    _insert_message(conn, "ses_p", "m2", "user", 2, {"text": "b"})
    _insert_message(conn, "ses_p", "m3", "user", 3, {"text": "c"})
    conn.commit()
    conn.close()
    before = db.read_bytes()
    _extract(db, monkeypatch, tmp_path)
    assert db.read_bytes() == before
    assert not db.with_suffix(db.suffix + "-wal").exists()
    assert not db.with_suffix(db.suffix + "-shm").exists()


def test_hostile_data_does_not_raise(monkeypatch, tmp_path):
    db = tmp_path / "hostile.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_hostile")
    conn.execute(
        "UPDATE session_v2 SET time_created = ?, time_updated = ? WHERE id = ?",
        (float("inf"), float("nan"), "ses_hostile"),
    )
    _insert_message(conn, "ses_hostile", "bad_json", "user", 1, {"text": "ok"})
    conn.execute(
        "UPDATE session_message SET data = ? WHERE id = ?",
        ("not-json{{{", "bad_json"),
    )
    _insert_message(conn, "ses_hostile", "list_json", "user", 2, {"text": "x"})
    conn.execute(
        "UPDATE session_message SET data = ? WHERE id = ?",
        (json.dumps([1, 2, 3]), "list_json"),
    )
    huge = "Z" * 150_000
    _insert_message(conn, "ses_hostile", "huge", "user", 3, {"text": huge})
    secret_tool = {
        "content": [
            {
                "type": "tool",
                "id": "s1",
                "name": "configure",
                "state": {
                    "status": "completed",
                    "input": {
                        "api_key": _SENTINEL,
                        "nested_secret_token": _SENTINEL,
                        "safe": "visible",
                    },
                },
            }
        ],
    }
    _insert_message(conn, "ses_hostile", "sec", "assistant", 4, secret_tool)
    for seq in range(5, 5 + 5001):
        _insert_message(conn, "ses_hostile", f"fill_{seq}", "user", seq, {"text": "x"})
    conn.commit()
    conn.close()

    sessions = _extract(db, monkeypatch, tmp_path)
    assert len(sessions) == 1
    blob = json.dumps(sessions[0], default=str)
    assert _SENTINEL not in blob
    assistant = next(m for m in sessions[0].messages if m.role == Role.ASSISTANT)
    assert _SENTINEL not in assistant.content
    assert "safe=visible" in assistant.content


def test_injected_context_not_counted_as_user_prompts(monkeypatch, tmp_path):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    monkeypatch.delenv("LORE_IMPORT_PROFILE", raising=False)
    db = tmp_path / "prompts.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_prompt")
    _insert_message(conn, "ses_prompt", "syn", "synthetic", 1, {"text": "ctx"})
    _insert_message(conn, "ses_prompt", "u1", "user", 2, {"text": "one"})
    _insert_message(conn, "ses_prompt", "u2", "user", 3, {"text": "two"})
    conn.commit()
    conn.close()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(db))
    extractor = OpenCodeExtractor(force_full=True)
    sessions = list(extractor.extract_sessions())
    assert sessions == []


def test_unknown_type_counted_once_in_discovery(monkeypatch, tmp_path):
    db = tmp_path / "unknown.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_u")
    for i in range(3):
        _insert_message(conn, "ses_u", f"u{i}", "user", i + 1, {"text": f"q{i}"})
    _insert_message(conn, "ses_u", "x1", "weird-a", 4, {})
    _insert_message(conn, "ses_u", "x2", "weird-a", 5, {})
    conn.commit()
    conn.close()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(db))
    extractor = OpenCodeExtractor(force_full=True)
    list(extractor.extract_sessions())
    assert extractor.discovery["records_unknown"] == 1


def test_tool_output_kept_as_tool_message_after_assistant(monkeypatch, tmp_path):
    db = tmp_path / "v2.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_out")
    _insert_message(conn, "ses_out", "msg_u", "user", 1, {"text": "run it"})
    block = {
        "type": "tool",
        "id": "t1",
        "name": "calc",
        "state": {"status": "completed", "input": {}, "content": [{"type": "text", "text": "42"}]},
    }
    _insert_message(conn, "ses_out", "msg_a", "assistant", 2, {"content": [block]})
    conn.commit()
    conn.close()

    sessions = _extract(db, monkeypatch, tmp_path)

    roles = [(m.role.value, m.content) for m in sessions[0].messages]
    assert roles[-1] == ("tool", "42")
    assert roles[-2][0] == "assistant"


def test_huge_integer_timestamp_does_not_abort_database(monkeypatch, tmp_path):
    db = tmp_path / "v2.db"
    conn = _create_v2_db(db)
    _insert_session(conn, "ses_big")
    conn.execute("UPDATE session_v2 SET title = ?", ("T" * 50_000,))
    payload = '{"text": "hi", "time": {"created": ' + "9" * 400 + "}}"
    conn.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES ('msg_h', 'ses_big', 'user', 1, 1, 1, ?)",
        (payload,),
    )
    _insert_session(conn, "ses_after")
    _insert_message(conn, "ses_after", "msg_ok", "user", 1, {"text": "still here"})
    conn.commit()
    conn.close()

    sessions = _extract(db, monkeypatch, tmp_path)

    by_id = {s.session_id: s for s in sessions}
    assert "ses_after" in by_id
    assert len(by_id["ses_big"].title) <= 200
