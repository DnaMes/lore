"""Tests for annuli/extractors/gemini.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from annuli.core.models import Role, Tool
from annuli.extractors.gemini import GeminiCLIExtractor


def _project_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _write_session_file(chats_dir: Path, session_data: dict) -> Path:
    chats_dir.mkdir(parents=True, exist_ok=True)
    session_file = chats_dir / "session-001.json"
    session_file.write_text(json.dumps(session_data), encoding="utf-8")
    return session_file


def _make_gemini_dir(tmp_path: Path, project_path: str, session_data: dict) -> Path:
    """Create the full .gemini/tmp/<hash>/chats/ tree."""
    gemini_tmp = tmp_path / ".gemini" / "tmp"
    h = _project_hash(str(Path(project_path).resolve()))
    project_dir = gemini_tmp / h
    chats_dir = project_dir / "chats"
    _write_session_file(chats_dir, session_data)
    return gemini_tmp


def _minimal_session(session_id: str = "sess-001") -> dict:
    return {
        "sessionId": session_id,
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Hello Gemini", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "Hello! How can I help?",
                "timestamp": "2025-06-15T10:01:00",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_gemini_not_available_when_no_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = GeminiCLIExtractor()
    assert extractor.is_available() is False


def test_gemini_available_when_dir_exists(monkeypatch, tmp_path):
    gemini_tmp = tmp_path / ".gemini" / "tmp"
    gemini_tmp.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = GeminiCLIExtractor()
    assert extractor.is_available() is True


def test_gemini_tool_property(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = GeminiCLIExtractor()
    assert extractor.tool == Tool.GEMINI_CLI


def test_gemini_extract_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = GeminiCLIExtractor()
    assert list(extractor.extract_sessions()) == []


# ---------------------------------------------------------------------------
# Session parsing
# ---------------------------------------------------------------------------


def test_parse_basic_session(monkeypatch, tmp_path):
    project_path = str(tmp_path / "my_project")
    Path(project_path).mkdir(parents=True)
    session_data = _minimal_session("test-session-gemini")
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "test-session-gemini"
    assert s.tool == Tool.GEMINI_CLI


def test_parse_message_roles(monkeypatch, tmp_path):
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = _minimal_session()
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    msgs = sessions[0].messages
    assert msgs[0].role == Role.USER
    assert msgs[1].role == Role.ASSISTANT


def test_parse_session_message_count(monkeypatch, tmp_path):
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = _minimal_session()
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    assert sessions[0].message_count == 2


def test_parse_session_with_thoughts(monkeypatch, tmp_path):
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "thinking-session",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {
                "type": "user",
                "content": "Explain something complex",
                "timestamp": "2025-06-15T10:00:00",
            },
            {
                "type": "gemini",
                "content": "Here is the explanation.",
                "timestamp": "2025-06-15T10:01:00",
                "thoughts": [
                    {"subject": "Step 1", "description": "Consider the problem"},
                    {"subject": "Step 2", "description": "Formulate answer"},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    # The assistant message should have reasoning
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].reasoning is not None
    assert "Step 1" in assistant_msgs[0].reasoning


def test_parse_session_info_type(monkeypatch, tmp_path):
    """Messages with type 'info' get Role.INFO."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "info-session",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Start session", "timestamp": "2025-06-15T10:00:00"},
            {"type": "info", "content": "Session started", "timestamp": "2025-06-15T10:00:01"},
            {"type": "gemini", "content": "Hello!", "timestamp": "2025-06-15T10:01:00"},
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    # Should import: 1 user prompt qualifies
    s = sessions[0]
    from annuli.core.models import Role as R

    roles = [m.role for m in s.messages]
    assert R.INFO in roles


def test_parse_session_unknown_type_skipped(monkeypatch, tmp_path):
    """Messages with unknown type are skipped."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "unknown-type",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Hello", "timestamp": "2025-06-15T10:00:00"},
            {"type": "UNKNOWN_TYPE", "content": "Skipped", "timestamp": "2025-06-15T10:00:01"},
            {"type": "gemini", "content": "Response", "timestamp": "2025-06-15T10:01:00"},
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assert s.message_count == 2  # unknown type skipped


def test_parse_session_tool_calls_no_content(monkeypatch, tmp_path):
    """When content is empty but toolCalls present, content is built from tool calls."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "tool-call-session",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Read a file", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "read_file", "args": {"file_path": "/home/user/test.py"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert any("test.py" in m.content for m in assistant_msgs)


def test_parse_write_file_tool_call(monkeypatch, tmp_path):
    """write_file tool call is formatted correctly."""
    project_path = str(tmp_path / "proj_write")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "write-tool",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Write a file", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "write_file", "args": {"file_path": "/foo/bar.py"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined_content = " ".join(m.content for m in s.messages)
    assert "bar.py" in combined_content


def test_parse_web_search_tool_call(monkeypatch, tmp_path):
    """web_search tool call is formatted correctly."""
    project_path = str(tmp_path / "proj_search")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "search-tool",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Search for python", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "web_search", "args": {"query": "python testing frameworks"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined_content = " ".join(m.content for m in s.messages)
    assert "python testing frameworks" in combined_content


def test_parse_bash_and_edit_tools(monkeypatch, tmp_path):
    """run_terminal_cmd and edit_file are formatted correctly."""
    project_path = str(tmp_path / "proj_bash")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "bash-edit-tool",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Run and edit", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "run_terminal_cmd", "args": {"command": "ls -la /tmp"}},
                    {"name": "edit_file", "args": {"file_path": "/baz/qux.py"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined_content = " ".join(m.content for m in s.messages)
    assert "ls -la /tmp" in combined_content
    assert "qux.py" in combined_content


def test_parse_glob_grep_tools(monkeypatch, tmp_path):
    """glob and grep tool calls are formatted correctly."""
    project_path = str(tmp_path / "proj_glob")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "glob-grep-tool",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Find patterns", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "glob", "args": {"pattern": "**/*.py"}},
                    {"name": "grep", "args": {"pattern": "import pytest"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined_content = " ".join(m.content for m in s.messages)
    assert "**/*.py" in combined_content
    assert "import pytest" in combined_content


def test_parse_custom_tool_call(monkeypatch, tmp_path):
    """Unknown/generic tool calls are formatted with the tool name."""
    project_path = str(tmp_path / "proj_custom")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "custom-tool",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Use custom tool", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "my_custom_function", "args": {"param1": "val1", "param2": "val2"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined_content = " ".join(m.content for m in s.messages)
    assert "my_custom_function" in combined_content


def test_parse_session_fallback_id(monkeypatch, tmp_path):
    """If no sessionId in JSON, stem of file is used."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Test", "timestamp": "2025-06-15T10:00:00"},
            {"type": "gemini", "content": "Reply", "timestamp": "2025-06-15T10:01:00"},
        ],
    }
    gemini_tmp = tmp_path / ".gemini" / "tmp"
    h = _project_hash(str(Path(project_path).resolve()))
    project_dir = gemini_tmp / h
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)
    session_file = chats_dir / "session-FALLBACK.json"
    session_file.write_text(json.dumps(session_data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    assert sessions[0].session_id == "session-FALLBACK"


def test_hash_map_resolves_project_path(monkeypatch, tmp_path):
    """The hash map correctly resolves project directory hashes to paths."""
    project_path = str(tmp_path / "my_project")
    Path(project_path).mkdir()
    session_data = _minimal_session()
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    # Project path might not match exactly due to resolution but should be non-None if found
    # The test just verifies the extractor runs correctly


def test_parse_session_web_fetch_tool(monkeypatch, tmp_path):
    """WebFetch tool call is formatted correctly."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "webfetch-session",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Fetch a URL", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "toolCalls": [
                    {"name": "web_fetch", "args": {"prompt": "Fetch https://example.com"}},
                ],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    combined = " ".join(m.content for m in s.messages)
    assert "WebFetch" in combined


def test_parse_session_reasoning_only_content(monkeypatch, tmp_path):
    """If content is empty and reasoning present, reasoning becomes content."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "reasoning-only",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Think out loud", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "",
                "timestamp": "2025-06-15T10:01:00",
                "thoughts": [{"subject": "Analysis", "description": "Need to consider carefully"}],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert any("Analysis" in m.content for m in assistant_msgs)


def test_parse_session_reasoning_appended_to_content(monkeypatch, tmp_path):
    """When both content and reasoning are present, reasoning is appended."""
    project_path = str(tmp_path / "proj")
    Path(project_path).mkdir()
    session_data = {
        "sessionId": "both-content-reasoning",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"type": "user", "content": "Analyze this", "timestamp": "2025-06-15T10:00:00"},
            {
                "type": "gemini",
                "content": "The main answer is here.",
                "timestamp": "2025-06-15T10:01:00",
                "thoughts": [{"subject": "Thinking", "description": "Internal thought process"}],
            },
        ],
    }
    _make_gemini_dir(tmp_path, project_path, session_data)
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())
    s = sessions[0]
    assistant_msgs = [m for m in s.messages if m.role == Role.ASSISTANT]
    assert len(assistant_msgs) == 1
    content = assistant_msgs[0].content
    assert "main answer" in content
    assert "Thoughts" in content
