"""Tests for annuli/extractors/copilot.py.

GitHub Copilot CLI writes one ``.jsonl`` file per session under
``~/.copilot/session-state/``. Each line is a record with a ``type``
(``session.start``, ``session.info``, ``user.message``,
``assistant.message``), a ``timestamp`` and a ``data`` payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from annuli.core.models import Role, Tool
from annuli.extractors.copilot import CopilotCLIExtractor


def _session_state_dir(home: Path) -> Path:
    path = home / ".copilot" / "session-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_session(home: Path, name: str, records: list[dict]) -> Path:
    path = _session_state_dir(home) / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _basic_records(session_id: str = "sess-001") -> list[dict]:
    return [
        {
            "type": "session.start",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"sessionId": session_id},
        },
        {
            "type": "user.message",
            "id": "u-1",
            "timestamp": "2025-06-15T10:00:05",
            "data": {"content": "How do I list files?"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:10",
            "data": {"messageId": "a-1", "content": "Use the ls command."},
        },
    ]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_not_available_when_no_session_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CopilotCLIExtractor().is_available() is False


def test_available_when_session_state_exists(monkeypatch, tmp_path):
    _session_state_dir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CopilotCLIExtractor().is_available() is True


def test_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(CopilotCLIExtractor().extract_sessions()) == []


def test_tool_property_is_copilot_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CopilotCLIExtractor().tool is Tool.COPILOT_CLI


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_basic_session(monkeypatch, tmp_path):
    _write_session(tmp_path, "file-stem", _basic_records("sess-xyz"))
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool is Tool.COPILOT_CLI
    # session.start record overrides the filename-derived id.
    assert s.session_id == "sess-xyz"
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[0].content == "How do I list files?"
    assert s.messages[1].role is Role.ASSISTANT
    assert s.messages[1].content == "Use the ls command."


def test_session_id_falls_back_to_filename(monkeypatch, tmp_path):
    """Without a session.start record, the file stem is the session id."""
    records = [
        {
            "type": "user.message",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"content": "a prompt"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:05",
            "data": {"content": "a reply"},
        },
    ]
    _write_session(tmp_path, "fallback-id", records)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "fallback-id"


def test_message_ids_preserved(monkeypatch, tmp_path):
    _write_session(tmp_path, "ids", _basic_records())
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    msgs = sessions[0].messages
    assert msgs[0].message_id == "u-1"
    assert msgs[1].message_id == "a-1"


def test_project_path_from_session_info(monkeypatch, tmp_path):
    records = [
        {
            "type": "session.start",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"sessionId": "with-folder"},
        },
        {
            "type": "session.info",
            "timestamp": "2025-06-15T10:00:01",
            "data": {"message": "Folder /home/user/coolrepo has been added to trust"},
        },
        {
            "type": "user.message",
            "timestamp": "2025-06-15T10:00:05",
            "data": {"content": "a prompt that is long enough"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:10",
            "data": {"content": "a reply"},
        },
    ]
    _write_session(tmp_path, "folder-sess", records)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].project_path == "/home/user/coolrepo"


def test_assistant_tool_requests_captured(monkeypatch, tmp_path):
    records = [
        {
            "type": "user.message",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"content": "run a shell command for me"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:05",
            "data": {
                "content": "Running it now.",
                "toolRequests": [
                    {
                        "toolCallId": "call-1",
                        "name": "shell",
                        "arguments": {"command": "ls -la"},
                    }
                ],
            },
        },
    ]
    _write_session(tmp_path, "tooly", records)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    assistant = [m for m in sessions[0].messages if m.role is Role.ASSISTANT][0]
    assert len(assistant.tool_calls) == 1
    assert assistant.tool_calls[0]["name"] == "shell"
    assert assistant.tool_calls[0]["id"] == "call-1"
    assert assistant.tool_calls[0]["input"] == {"command": "ls -la"}


def test_assistant_message_with_only_tool_request_kept(monkeypatch, tmp_path):
    """An assistant turn with no text but a tool request is still a message."""
    records = [
        {
            "type": "user.message",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"content": "please read the config file now"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:05",
            "data": {
                "content": "",
                "toolRequests": [{"toolCallId": "c1", "name": "read_file"}],
            },
        },
    ]
    _write_session(tmp_path, "toolonly", records)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].message_count == 2


def test_empty_messages_skipped(monkeypatch, tmp_path):
    records = [
        {
            "type": "user.message",
            "timestamp": "2025-06-15T10:00:00",
            "data": {"content": "the only real message here"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-15T10:00:05",
            "data": {"content": ""},
        },
    ]
    _write_session(tmp_path, "emptyasst", records)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    # Empty assistant message with no tool requests is dropped.
    assert sessions[0].message_count == 1


def test_corrupt_lines_skipped(monkeypatch, tmp_path):
    path = _session_state_dir(tmp_path) / "corrupt.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user.message",
                "timestamp": "2025-06-15T10:00:00",
                "data": {"content": "valid prompt"},
            }
        )
        + "\nGARBAGE LINE\n"
        + json.dumps(
            {
                "type": "assistant.message",
                "timestamp": "2025-06-15T10:00:05",
                "data": {"content": "valid reply"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].message_count == 2


def test_timestamps_span_first_and_last_record(monkeypatch, tmp_path):
    _write_session(tmp_path, "tsspan", _basic_records())
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    s = sessions[0]
    assert s.created_at <= s.last_updated


def test_extract_multiple_sessions(monkeypatch, tmp_path):
    for i in range(3):
        _write_session(tmp_path, f"sess-{i}", _basic_records(f"id-{i}"))
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CopilotCLIExtractor().extract_sessions())
    assert len(sessions) == 3
    assert {s.session_id for s in sessions} == {"id-0", "id-1", "id-2"}
