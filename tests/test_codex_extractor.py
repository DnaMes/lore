"""Tests for lore/extractors/codex.py."""

from __future__ import annotations

import json
from pathlib import Path

from lore.core.models import Role, Tool
from lore.extractors.codex import CodexExtractor


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _minimal_session_records(session_id: str = "test-session-01") -> list[dict]:
    ts = "2025-06-15T10:00:00"
    return [
        {
            "type": "session_meta",
            "timestamp": ts,
            "payload": {"id": session_id, "cwd": "/home/user/projects/foo", "cli_version": "1.2.3"},
        },
        {
            "type": "message",
            "timestamp": ts,
            "payload": {
                "role": "user",
                "content": "Hello, implement a quick sort",
                "id": "msg-001",
            },
        },
        {
            "type": "message",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "role": "assistant",
                "content": "Sure, here is quicksort.",
                "id": "msg-002",
            },
        },
    ]


# ---------------------------------------------------------------------------
# Extractor availability
# ---------------------------------------------------------------------------


def test_codex_extractor_not_available_when_no_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = CodexExtractor()
    assert extractor.is_available() is False


def test_codex_extractor_available_when_dir_exists(monkeypatch, tmp_path):
    sessions_dir = tmp_path / ".codex" / "sessions"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = CodexExtractor()
    assert extractor.is_available() is True


def test_codex_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    assert sessions == []


# ---------------------------------------------------------------------------
# Parsing a rollout JSONL file
# ---------------------------------------------------------------------------


def _make_session_file(tmp_path: Path, records: list[dict]) -> tuple[Path, Path]:
    """Create dir structure and a rollout file, return rollout path + sessions dir."""
    sessions_dir = tmp_path / ".codex" / "sessions" / "2025" / "06" / "15"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-abc123.jsonl"
    _write_jsonl(rollout, records)
    return rollout, sessions_dir


def test_parse_basic_session(monkeypatch, tmp_path):
    records = _minimal_session_records("session-xyz")
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())

    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "session-xyz"
    assert s.tool == Tool.CODEX
    assert s.project_path == "/home/user/projects/foo"
    assert s.cli_version == "1.2.3"


def test_parse_messages_count(monkeypatch, tmp_path):
    records = _minimal_session_records()
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert s.message_count == 2
    assert s.user_prompt_count == 1


def test_parse_message_roles(monkeypatch, tmp_path):
    records = _minimal_session_records()
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    msgs = sessions[0].messages
    assert msgs[0].role == Role.USER
    assert msgs[1].role == Role.ASSISTANT


def test_parse_content_as_list(monkeypatch, tmp_path):
    """Message content can be a list of dicts with 'type':'text'."""
    ts = "2025-06-15T10:00:00"
    records = [
        {
            "type": "message",
            "timestamp": ts,
            "payload": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                    {"type": "image", "data": "base64..."},
                ],
                "id": "msg-001",
            },
        }
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert "part one" in s.messages[0].content
    assert "part two" in s.messages[0].content


def test_parse_response_item_message(monkeypatch, tmp_path):
    ts = "2025-06-15T10:00:00"
    records = [
        {
            "type": "message",
            "timestamp": ts,
            "payload": {"role": "user", "content": "What is quicksort?", "id": "u-001"},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "resp-001",
                "content": [{"type": "output_text", "text": "Here is the answer."}],
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert any("Here is the answer" in msg.content for msg in s.messages)


def test_parse_response_item_reasoning(monkeypatch, tmp_path):
    ts = "2025-06-15T10:00:00"
    records = [
        {
            "type": "message",
            "timestamp": ts,
            "payload": {"role": "user", "content": "Explain reasoning", "id": "u-r1"},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "I should think carefully."}],
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert any("I should think carefully" in msg.content for msg in s.messages)


def _user_record(ts: str = "2025-06-15T10:00:00") -> dict:
    return {
        "type": "message",
        "timestamp": ts,
        "payload": {"role": "user", "content": "Run the command please", "id": "u-cmd"},
    }


def test_parse_response_item_function_call_shell(monkeypatch, tmp_path):
    ts = "2025-06-15T10:00:00"
    records = [
        _user_record(ts),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": json.dumps({"command": "ls -la"}),
                "call_id": "call-001",
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert any("ls -la" in msg.content for msg in s.messages)


def test_parse_response_item_function_call_read_file(monkeypatch, tmp_path):
    ts = "2025-06-15T10:00:00"
    records = [
        _user_record(ts),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": json.dumps({"path": "/home/user/foo.py"}),
                "call_id": "call-002",
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert any("foo.py" in msg.content for msg in s.messages)


def test_parse_response_item_function_call_generic(monkeypatch, tmp_path):
    ts = "2025-06-15T10:00:00"
    records = [
        _user_record(ts),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "some_other_tool",
                "arguments": json.dumps({"key": "value"}),
                "call_id": "call-003",
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert any("some_other_tool" in msg.content for msg in s.messages)


def test_parse_session_fallback_id_when_no_meta(monkeypatch, tmp_path):
    """If no session_meta record, session_id falls back to stem of file."""
    ts = "2025-06-15T10:00:00"
    records = [
        {
            "type": "message",
            "timestamp": ts,
            "payload": {"role": "user", "content": "test", "id": "m1"},
        }
    ]
    sessions_dir = tmp_path / ".codex" / "sessions" / "2025" / "06" / "15"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-fallback123.jsonl"
    _write_jsonl(rollout, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    assert sessions[0].session_id == "rollout-fallback123"


def test_parse_invalid_json_lines_skipped(monkeypatch, tmp_path):
    """Corrupt lines should be skipped without crashing."""
    sessions_dir = tmp_path / ".codex" / "sessions" / "2025" / "06" / "15"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-corrupt.jsonl"
    rollout.write_text(
        '{"type": "message", "timestamp": "2025-06-15T10:00:00", "payload": {"role": "user", "content": "ok", "id": "m1"}}\n'
        "NOT VALID JSON HERE\n"
        '{"type": "message", "timestamp": "2025-06-15T10:01:00", "payload": {"role": "assistant", "content": "reply", "id": "m2"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].message_count == 2


def test_parse_timestamps_set_created_and_updated(monkeypatch, tmp_path):
    records = _minimal_session_records()
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert s.created_at <= s.last_updated


def test_extract_multiple_sessions(monkeypatch, tmp_path):
    """Multiple JSONL files result in multiple sessions."""
    sessions_dir = tmp_path / ".codex" / "sessions" / "2025" / "06" / "15"
    sessions_dir.mkdir(parents=True)

    for i in range(3):
        records = _minimal_session_records(f"session-{i}")
        rollout = sessions_dir / f"rollout-{i:04d}.jsonl"
        _write_jsonl(rollout, records)

    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 3
    ids = {s.session_id for s in sessions}
    assert ids == {"session-0", "session-1", "session-2"}


def test_codex_tool_property(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = CodexExtractor()
    assert extractor.tool == Tool.CODEX


def test_parse_function_call_bad_json_args(monkeypatch, tmp_path):
    """Malformed arguments JSON in function call should not crash."""
    ts = "2025-06-15T10:00:00"
    records = [
        _user_record(ts),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": "NOT VALID JSON {{",
                "call_id": "call-bad",
            },
        },
    ]
    _make_session_file(tmp_path, records)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    # Should still yield a session with the tool call message
    assert len(sessions) == 1


# ---------------------------------------------------------------------------
# Pre-parse source skip (#112): codex was the one big extractor that ignored it
# ---------------------------------------------------------------------------


def test_codex_unchanged_rollout_is_skipped_before_parsing(monkeypatch, tmp_path):
    rollout, _ = _make_session_file(tmp_path, _minimal_session_records())
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = CodexExtractor()
    assert [s.session_id for s in extractor.extract_sessions()] == ["test-session-01"]

    extractor = CodexExtractor()
    extractor.known_source_mtimes = {str(rollout): rollout.stat().st_mtime_ns}
    extractor.known_source_parser_versions = {str(rollout): extractor.PARSER_VERSION}
    assert list(extractor.extract_sessions()) == []
    assert extractor.skipped_sources == {str(rollout)}

    rollout.touch()
    extractor.known_source_mtimes = {str(rollout): 1}
    assert [s.session_id for s in extractor.extract_sessions()] == ["test-session-01"]
