import json

from annuli.core.models import Role, Tool
from annuli.extractors.claude import ClaudeCodeExtractor
from annuli.extractors.codex import CodexExtractor
from annuli.extractors.gemini import GeminiCLIExtractor


def test_claude_parse_session_contract(tmp_path):
    session_file = tmp_path / "session.jsonl"
    records = [
        {"type": "summary", "summary": "Refactor parser"},
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:00",
            "message": {"content": "Please refactor this"},
            "sessionId": "ses_claude_1",
            "uuid": "u1",
        },
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:01:00",
            "message": {"content": [{"type": "text", "text": "Done."}]},
            "sessionId": "ses_claude_1",
            "uuid": "a1",
        },
    ]
    session_file.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    extractor = ClaudeCodeExtractor()
    session = extractor._parse_session(session_file, "/repo/project")

    assert session.tool == Tool.CLAUDE_CODE
    assert session.session_id == "ses_claude_1"
    assert session.title == "Refactor parser"
    assert len(session.messages) == 2
    assert session.messages[0].role == Role.USER
    assert session.messages[1].role == Role.ASSISTANT


def test_claude_sanitizes_kryptic_summary_title(tmp_path):
    session_file = tmp_path / "session-kryptic.jsonl"
    records = [
        {
            "type": "summary",
            "summary": "<command-name>login</command-name> <command-message>login</command-message>",
        },
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:00",
            "message": {"content": "Please debug this issue"},
            "sessionId": "ses_claude_2",
            "uuid": "u1",
        },
    ]
    session_file.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    extractor = ClaudeCodeExtractor()
    session = extractor._parse_session(session_file, "/repo/project")

    assert session.title is None
    assert session.summary is None


def test_gemini_parse_session_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(GeminiCLIExtractor, "_build_hash_map", lambda self: None)
    session_file = tmp_path / "session-1.json"
    payload = {
        "sessionId": "ses_gemini_1",
        "startTime": "2026-03-01T09:00:00",
        "lastUpdated": "2026-03-01T09:05:00",
        "messages": [
            {
                "id": "m1",
                "type": "user",
                "timestamp": "2026-03-01T09:00:00",
                "content": "show files",
            },
            {
                "id": "m2",
                "type": "gemini",
                "timestamp": "2026-03-01T09:01:00",
                "content": "",
                "toolCalls": [{"name": "run_terminal_cmd", "args": {"command": "ls -la"}}],
            },
        ],
    }
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    extractor = GeminiCLIExtractor()
    session = extractor._parse_session_file(session_file, "/repo/project", "hash123")

    assert session.tool == Tool.GEMINI_CLI
    assert session.session_id == "ses_gemini_1"
    assert len(session.messages) == 2
    assert session.messages[1].role == Role.ASSISTANT
    assert "[Tool: Bash]" in session.messages[1].content


def test_codex_parse_session_contract_with_malformed_lines(tmp_path):
    session_file = tmp_path / "rollout-1.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-03-02T08:00:00",
                "payload": {"id": "ses_codex_1", "cwd": "/repo/project"},
            }
        ),
        "not-json",
        json.dumps(
            {
                "type": "message",
                "timestamp": "2026-03-02T08:01:00",
                "payload": {"id": "m1", "role": "user", "content": "help"},
            }
        ),
    ]
    session_file.write_text("\n".join(lines), encoding="utf-8")

    extractor = CodexExtractor()
    session = extractor._parse_session_file(session_file)

    assert session.tool == Tool.CODEX
    assert session.session_id == "ses_codex_1"
    assert len(session.messages) == 1
    assert session.messages[0].role == Role.USER
