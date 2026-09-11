"""Extended tests for lore/extractors/opencode.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lore.core.models import Role, Tool
from lore.extractors.opencode import LoadStats, OpenCodeExtractor

# ---------------------------------------------------------------------------
# Helpers for building OpenCode storage layout
# ---------------------------------------------------------------------------


def _make_storage(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    """Create .local/share/opencode/storage directory layout, return (session_dir, message_dir, part_dir)."""
    storage = tmp_path / ".local" / "share" / "opencode" / "storage"
    session_dir = storage / "session"
    message_dir = storage / "message"
    part_dir = storage / "part"
    for d in [session_dir, message_dir, part_dir]:
        d.mkdir(parents=True)
    return session_dir, message_dir, part_dir


def _write_session(
    session_dir: Path, session_id: str, project: str | None = None, title: str | None = None
) -> Path:
    ts_ms = 1700000000000  # fixed timestamp in ms
    data = {
        "id": session_id,
        "projectID": "proj-hash-001",
        "directory": project,
        "title": title,
        "version": "0.1.0",
        "time": {"created": ts_ms, "updated": ts_ms + 60000},
    }
    f = session_dir / f"ses_{session_id}.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _write_message(message_dir: Path, session_id: str, message_id: str, role: str = "user") -> Path:
    ts_ms = 1700000000000
    msg_dir = message_dir / session_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": message_id,
        "sessionID": session_id,
        "role": role,
        "time": {"created": ts_ms},
        "model": {"providerID": "anthropic", "modelID": "claude-sonnet-4"},
        "tokens": {"input": 100, "output": 200},
    }
    f = msg_dir / f"msg_{message_id}.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _write_part(
    part_dir: Path,
    message_id: str,
    part_id: str,
    part_type: str = "text",
    text: str = "Hello from test",
) -> Path:
    part_d = part_dir / message_id
    part_d.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "id": part_id,
        "messageID": message_id,
        "type": part_type,
        "time": {"start": 1700000000000},
    }
    if part_type == "text":
        data["text"] = text
    elif part_type == "reasoning":
        data["text"] = text
    elif part_type == "tool":
        data["tool"] = "bash"
        data["callID"] = f"call-{part_id}"
        data["state"] = {
            "input": {"command": "ls -la"},
            "output": "file1.txt file2.txt",
            "status": "completed",
        }
    f = part_d / f"prt_{part_id}.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------


def test_opencode_not_available_when_no_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    assert extractor.is_available() is False


def test_opencode_available_when_storage_exists(monkeypatch, tmp_path):
    _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    assert extractor.is_available() is True


def test_opencode_tool_property(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    assert extractor.tool == Tool.OPENCODE


def test_opencode_extract_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert sessions == []


# ---------------------------------------------------------------------------
# Full file-based extraction
# ---------------------------------------------------------------------------


def test_extract_single_session(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-001", project="/home/user/myapp", title="My Session")
    _write_message(message_dir, "sess-001", "msg-001", role="user")
    _write_part(part_dir, "msg-001", "prt-001", text="How do I implement quicksort?")
    _write_message(message_dir, "sess-001", "msg-002", role="assistant")
    _write_part(part_dir, "msg-002", "prt-002", text="Here is quicksort implementation.")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sess-001"
    assert s.tool == Tool.OPENCODE
    assert s.title == "My Session"
    assert s.project_path == "/home/user/myapp"


def test_extract_message_roles(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-roles")
    _write_message(message_dir, "sess-roles", "msg-u1", role="user")
    _write_part(part_dir, "msg-u1", "prt-u1", text="User question here")
    _write_message(message_dir, "sess-roles", "msg-a1", role="assistant")
    _write_part(part_dir, "msg-a1", "prt-a1", text="Assistant answer here")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert s.message_count >= 2
    roles = {m.role for m in s.messages}
    assert Role.USER in roles
    assert Role.ASSISTANT in roles


def test_extract_message_content(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-content")
    _write_message(message_dir, "sess-content", "msg-001", role="user")
    _write_part(part_dir, "msg-001", "prt-001", text="Implement a binary search algorithm")
    _write_message(message_dir, "sess-content", "msg-002", role="assistant")
    _write_part(part_dir, "msg-002", "prt-002", text="Here is binary search in Python:")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    all_content = " ".join(m.content for m in s.messages)
    assert "binary search" in all_content.lower()


def test_extract_reasoning_parts(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-reasoning")
    _write_message(message_dir, "sess-reasoning", "msg-u1", role="user")
    _write_part(part_dir, "msg-u1", "prt-u1", text="Think deeply about this problem")
    _write_message(message_dir, "sess-reasoning", "msg-a1", role="assistant")
    _write_part(part_dir, "msg-a1", "prt-text", text="The answer is 42")
    _write_part(
        part_dir,
        "msg-a1",
        "prt-reasoning",
        part_type="reasoning",
        text="I need to consider carefully",
    )

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert any(m.reasoning == "I need to consider carefully" for m in assistant_msgs)


def test_extract_tool_parts(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-tools")
    _write_message(message_dir, "sess-tools", "msg-u1", role="user")
    _write_part(part_dir, "msg-u1", "prt-u1", text="Run ls please")
    _write_message(message_dir, "sess-tools", "msg-a1", role="assistant")
    _write_part(part_dir, "msg-a1", "prt-tool", part_type="tool", text="")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert any(len(m.tool_calls) > 0 for m in assistant_msgs)


def test_extract_multiple_sessions(monkeypatch, tmp_path):
    session_dir, message_dir, part_dir = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    for i in range(3):
        sess_id = f"sess-multi-{i:03d}"
        _write_session(session_dir, sess_id, project=f"/proj/{i}")
        _write_message(message_dir, sess_id, f"msg-{i}-u", role="user")
        _write_part(part_dir, f"msg-{i}-u", f"prt-{i}-u", text=f"Question {i} from user")
        _write_message(message_dir, sess_id, f"msg-{i}-a", role="assistant")
        _write_part(part_dir, f"msg-{i}-a", f"prt-{i}-a", text=f"Answer {i} from assistant")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 3


def test_extract_session_with_no_messages(monkeypatch, tmp_path):
    """Sessions with no messages are filtered by should_import_session."""
    session_dir, *_ = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_session(session_dir, "sess-empty")
    # No messages written

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 0


def test_extract_session_missing_id_skipped(monkeypatch, tmp_path):
    """Session files without 'id' are skipped."""
    session_dir, *_ = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    bad_file = session_dir / "ses_noid.json"
    bad_file.write_text(json.dumps({"title": "No ID session"}), encoding="utf-8")

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 0


def test_safe_load_json_bad_json(monkeypatch, tmp_path):
    """Corrupt JSON returns None and increments error counter."""
    session_dir, *_ = _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    bad = session_dir / "ses_corrupt.json"
    bad.write_text("THIS IS NOT JSON {{{{", encoding="utf-8")

    extractor = OpenCodeExtractor()
    result = extractor._safe_load_json(bad)
    assert result is None
    assert extractor.stats.errors_json_decode == 1


def test_safe_load_json_missing_file(monkeypatch, tmp_path):
    """Missing file returns None and increments error counter."""
    _make_storage(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = OpenCodeExtractor()
    result = extractor._safe_load_json(tmp_path / "nonexistent.json")
    assert result is None
    assert extractor.stats.errors_file_not_found == 1


# ---------------------------------------------------------------------------
# _assemble_message_content_from_parts (used by sqlite path)
# ---------------------------------------------------------------------------


def test_assemble_text_parts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {"type": "text", "text": "Hello world"},
        {"type": "text", "text": "Second paragraph"},
    ]
    content, reasoning, tool_calls = extractor._assemble_message_content_from_parts(parts)
    assert "Hello world" in content
    assert "Second paragraph" in content
    assert reasoning is None
    assert tool_calls == []


def test_assemble_reasoning_part(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {"type": "reasoning", "text": "I should think about this carefully"},
        {"type": "text", "text": "The answer is 42"},
    ]
    content, reasoning, _ = extractor._assemble_message_content_from_parts(parts)
    assert reasoning == "I should think about this carefully"
    assert "42" in content


def test_assemble_tool_part_with_input(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {
            "type": "tool",
            "tool": "bash",
            "callID": "call-123",
            "state": {
                "input": {"command": "ls -la", "cwd": "/tmp"},
                "output": "total 0",
                "status": "completed",
            },
        }
    ]
    content, _, tool_calls = extractor._assemble_message_content_from_parts(parts)
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "bash"
    assert "bash" in content


def test_assemble_tool_part_no_input(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {
            "type": "tool",
            "tool": "get_info",
            "callID": "call-456",
            "state": {"input": {}, "output": "some output", "status": "completed"},
        }
    ]
    content, *_ = extractor._assemble_message_content_from_parts(parts)
    assert "[Tool: get_info]" in content


def test_assemble_tool_truncates_large_output(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    large_output = "x" * (extractor.MAX_TOOL_PART_CHARS + 1000)
    parts = [
        {
            "type": "tool",
            "tool": "bash",
            "callID": "call-big",
            "state": {"input": {}, "output": large_output, "status": "completed"},
        }
    ]
    *_, tool_calls = extractor._assemble_message_content_from_parts(parts)
    assert "truncated" in tool_calls[0]["output"]


def test_assemble_step_markers_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {"type": "step-start"},
        {"type": "text", "text": "Actual content"},
        {"type": "step-finish"},
    ]
    content, _, _ = extractor._assemble_message_content_from_parts(parts)
    assert content == "Actual content"


def test_assemble_unknown_part_type_with_text(monkeypatch, tmp_path):
    """Unknown part types in _assemble_message_content_from_parts are silently ignored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {"type": "custom_type", "text": "Some custom text"},  # ignored (no else branch)
        {"type": "text", "text": "Normal content"},
    ]
    content, _, _ = extractor._assemble_message_content_from_parts(parts)
    # The sqlite path doesn't have an else clause for unknown types
    assert "Normal content" in content


def test_assemble_empty_parts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    content, reasoning, tool_calls = extractor._assemble_message_content_from_parts([])
    assert content == ""
    assert reasoning is None
    assert tool_calls == []


# ---------------------------------------------------------------------------
# State cache (incremental mode)
# ---------------------------------------------------------------------------


def test_should_skip_session_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = None
    assert extractor._should_skip_session("any-session", 12345) is False


def test_should_skip_session_not_in_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {}
    assert extractor._should_skip_session("new-session", 12345) is False


def test_should_skip_session_in_cache_same_timestamp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {"sess-123": 1700000000}
    assert extractor._should_skip_session("sess-123", 1700000000) is True


def test_should_skip_session_in_cache_older(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {"sess-123": 1700000000}
    assert extractor._should_skip_session("sess-123", 1699999999) is True


def test_should_skip_session_in_cache_newer(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {"sess-123": 1700000000}
    assert extractor._should_skip_session("sess-123", 1700000001) is False


def test_update_state_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {}
    extractor._update_state_cache("sess-abc", 1700000000)
    assert extractor._state_cache["sess-abc"] == 1700000000


def test_update_state_cache_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = None
    extractor._update_state_cache("sess-abc", 1700000000)  # should not raise


def test_load_state_cache_from_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()

    cache_data = {"sess-abc": 1700000000, "sess-def": 1700001000}
    extractor.cache_path.mkdir(parents=True, exist_ok=True)
    extractor.state_file.write_text(json.dumps(cache_data), encoding="utf-8")

    extractor._load_state_cache()
    assert extractor._state_cache == cache_data


def test_load_state_cache_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._load_state_cache()
    assert extractor._state_cache == {}


def test_save_state_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = {"sess-xyz": 1700000000}
    extractor._save_state_cache()

    assert extractor.state_file.exists()
    saved = json.loads(extractor.state_file.read_text())
    assert saved["sess-xyz"] == 1700000000


def test_save_state_cache_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    extractor._state_cache = None
    extractor._save_state_cache()  # should not raise, file not created


# ---------------------------------------------------------------------------
# LoadStats.print_summary
# ---------------------------------------------------------------------------


def test_loadstats_print_summary_no_errors(capsys):
    stats = LoadStats(
        sessions_found=10,
        sessions_loaded=8,
        sessions_skipped=2,
        messages_found=50,
        messages_loaded=45,
        parts_found=200,
        parts_loaded=195,
    )
    stats.print_summary()
    captured = capsys.readouterr()
    assert "10" in captured.err
    assert "8" in captured.err


def test_loadstats_print_summary_with_errors(capsys):
    stats = LoadStats(
        sessions_found=10,
        sessions_loaded=8,
        messages_found=50,
        messages_loaded=45,
        parts_found=200,
        parts_loaded=195,
        errors_file_not_found=2,
        errors_json_decode=1,
    )
    stats.print_summary()
    captured = capsys.readouterr()
    assert "FileNotFound=2" in captured.err


# ---------------------------------------------------------------------------
# SQLite extraction
# ---------------------------------------------------------------------------


def _create_opencode_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            time_created INTEGER,
            time_updated INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            message_id TEXT,
            time_created INTEGER,
            data TEXT
        )
    """)
    conn.commit()
    conn.close()


def _insert_sqlite_session(db_path: Path, session_id: str, directory: str | None = None) -> None:
    conn = sqlite3.connect(db_path)
    ts = 1700000000000
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        (session_id, "proj-hash", directory, "SQLite Session", "0.2.0", ts, ts + 60000),
    )
    conn.commit()
    conn.close()


def _insert_sqlite_message(
    db_path: Path, session_id: str, message_id: str, role: str, content: str
) -> None:
    conn = sqlite3.connect(db_path)
    ts = 1700000000000
    msg_data = {"id": message_id, "role": role, "time": {"created": ts}}
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        (message_id, session_id, ts, json.dumps(msg_data)),
    )
    part_data = {"id": f"prt-{message_id}", "type": "text", "text": content}
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        (f"prt-{message_id}", session_id, message_id, ts, json.dumps(part_data)),
    )
    conn.commit()
    conn.close()


def test_extract_from_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "opencode.db"
    _create_opencode_sqlite(db_path)
    _insert_sqlite_session(db_path, "sql-sess-001", directory="/home/user/proj")
    _insert_sqlite_message(
        db_path, "sql-sess-001", "sql-msg-u1", "user", "User question via sqlite"
    )
    _insert_sqlite_message(db_path, "sql-sess-001", "sql-msg-a1", "assistant", "Answer via sqlite")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(db_path))

    extractor = OpenCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sql-sess-001"
    assert s.tool == Tool.OPENCODE


def test_extract_sqlite_missing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SQLITE_PATHS", str(tmp_path / "nonexistent.db"))

    extractor = OpenCodeExtractor()
    result = extractor._extract_sessions_from_sqlite(tmp_path / "nonexistent.db")
    assert result == {}


def test_extract_sqlite_empty_db(monkeypatch, tmp_path):
    db_path = tmp_path / "empty.db"
    _create_opencode_sqlite(db_path)

    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._extract_sessions_from_sqlite(db_path)
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_session / _parse_message
# ---------------------------------------------------------------------------


def test_parse_session_no_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._parse_session(tmp_path / "fake.json", {"title": "No ID"})
    assert result is None


def test_parse_message_no_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._parse_message({"role": "user"})
    assert result is None


def test_parse_message_with_model_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._parse_message(
        {
            "id": "msg-with-model",
            "role": "assistant",
            "time": {"created": 1700000000000},
            "model": {"providerID": "openai", "modelID": "gpt-4o"},
        }
    )
    assert result is not None
    assert result.model == "openai/gpt-4o"


def test_parse_message_with_provider_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._parse_message(
        {
            "id": "msg-provider-fields",
            "role": "user",
            "time": {"created": 1700000000000},
            "providerID": "anthropic",
            "modelID": "claude-sonnet",
        }
    )
    assert result is not None
    assert result.model == "anthropic/claude-sonnet"


def test_parse_message_with_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    result = extractor._parse_message(
        {
            "id": "msg-tokens",
            "role": "assistant",
            "time": {"created": 1700000000000},
            "tokens": {"input": 500, "output": 1000, "reasoning": 200},
        }
    )
    assert result is not None
    assert result.tokens is not None
    assert result.tokens["input"] == 500
    assert result.tokens["output"] == 1000
    assert result.tokens["total"] == 1500
    assert result.tokens["reasoning"] == 200


def test_extract_tool_input_many_keys(monkeypatch, tmp_path):
    """Tool input with > 3 keys shows ellipsis in summary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor()
    parts = [
        {
            "type": "tool",
            "tool": "multi_arg_tool",
            "callID": "call-multi",
            "state": {
                "input": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
                "output": "done",
                "status": "completed",
            },
        }
    ]
    content, *_ = extractor._assemble_message_content_from_parts(parts)
    assert "..." in content


def test_clamp_tool_output_keeps_normal_output_full(monkeypatch, tmp_path):
    """#54 — outputs under the sanity cap are kept verbatim, not truncated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ex = OpenCodeExtractor()

    # 50k+ used to be truncated; now kept in full.
    text = "x" * 60_000
    out, truncated = ex._clamp_tool_output(text)
    assert truncated is False
    assert out == text


def test_clamp_tool_output_clamps_pathological_dump(monkeypatch, tmp_path):
    """#54 — only multi-MB pathological dumps are clamped, and the flag is set."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ex = OpenCodeExtractor()

    text = "y" * (ex.MAX_TOOL_PART_CHARS + 500)
    out, truncated = ex._clamp_tool_output(text)
    assert truncated is True
    assert out.startswith("y" * 100)
    assert f"truncated, {len(text)} chars total" in out


def test_clamp_tool_output_passes_non_strings_through(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ex = OpenCodeExtractor()
    obj = {"structured": "output"}
    out, truncated = ex._clamp_tool_output(obj)
    assert out is obj
    assert truncated is False


# ---------------------------------------------------------------------------
# Two-pass extraction, bounded memory (#104)
# ---------------------------------------------------------------------------


def test_two_pass_last_write_wins_across_roots(monkeypatch, tmp_path):
    """A later root with a newer time.updated supersedes an earlier root's
    copy — the winner decision must be identical to the old
    materialise-then-sort behaviour (#104)."""
    session_a, message_a, part_a = _make_storage(tmp_path / "a")
    storage_a = session_a.parent
    session_b = tmp_path / "b" / ".local" / "share" / "opencode" / "storage" / "session"
    message_b = session_b.parent / "message"
    part_b = session_b.parent / "part"
    for d in (session_b, message_b, part_b):
        d.mkdir(parents=True)

    # Same id in both roots; root B is newer (updated 2000 vs 1000 ms).
    data_old = {
        "id": "ses_dup",
        "directory": "/repo/old",
        "title": "Old copy",
        "version": "0.1.0",
        "time": {"created": 1700000000000, "updated": 1700000001000},
    }
    data_new = {
        "id": "ses_dup",
        "directory": "/repo/new",
        "title": "New copy",
        "version": "0.1.0",
        "time": {"created": 1700000000000, "updated": 1700000002000},
    }
    (session_a / "ses_ses_dup.json").write_text(json.dumps(data_old), encoding="utf-8")
    _write_message(message_a, "ses_dup", "msg_old_1", role="user")
    _write_part(part_a, "msg_old_1", "prt_old_1", text="Old copy question")
    (session_b / "ses_ses_dup.json").write_text(json.dumps(data_new), encoding="utf-8")
    _write_message(message_b, "ses_dup", "msg_new_1", role="user")
    _write_part(part_b, "msg_new_1", "prt_new_1", text="New copy question")

    # Unique sessions in each root to prove nothing gets lost.
    data_c = {
        "id": "ses_only_a",
        "directory": "/repo/a",
        "title": "Only A",
        "version": "0.1.0",
        "time": {"created": 1700000000500, "updated": 1700000000500},
    }
    (session_a / "ses_ses_only_a.json").write_text(json.dumps(data_c), encoding="utf-8")
    _write_message(message_a, "ses_only_a", "msg_only_a_1", role="user")
    _write_part(part_a, "msg_only_a_1", "prt_only_a_1", text="Only A question")

    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = OpenCodeExtractor(force_full=True)
    # Both roots must be scanned.
    monkeypatch.setattr(extractor, "storage_roots", [storage_a, session_b.parent], raising=False)

    sessions = list(extractor.extract_sessions())

    by_id = {s.session_id: s for s in sessions}
    # Winner for the duplicate is the newer root copy.
    assert by_id["ses_dup"].title == "New copy"
    assert by_id["ses_dup"].project_path == "/repo/new"
    # Nothing lost; sorted oldest-created first.
    assert len(sessions) == 2
    created = [s.created_at for s in sessions]
    assert created == sorted(created)
    # Stats count exactly what was yielded.
    assert extractor.stats.sessions_loaded == len(sessions)
