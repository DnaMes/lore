"""Tests for annuli/extractors/vscode.py.

VSCode Copilot writes chat sessions under
``~/.config/Code/User/workspaceStorage/<hash>/chatSessions/`` as either
legacy ``.json`` files (single object with a ``requests`` array) or new
``.jsonl`` files (one record per line, ``kind`` discriminates header /
request / response). A sibling ``workspace.json`` carries the project path.
"""

from __future__ import annotations

import json
from pathlib import Path

from annuli.core.models import Role, Tool
from annuli.extractors.vscode import VSCodeCopilotExtractor


def _chat_sessions_dir(home: Path, workspace: str = "ws-abc") -> Path:
    path = home / ".config" / "Code" / "User" / "workspaceStorage" / workspace / "chatSessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_workspace_json(home: Path, workspace: str, folder: str) -> None:
    ws_dir = home / ".config" / "Code" / "User" / "workspaceStorage" / workspace
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": folder}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_not_available_when_no_workspace_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert VSCodeCopilotExtractor().is_available() is False


def test_available_when_workspace_storage_exists(monkeypatch, tmp_path):
    (tmp_path / ".config" / "Code" / "User" / "workspaceStorage").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert VSCodeCopilotExtractor().is_available() is True


def test_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(VSCodeCopilotExtractor().extract_sessions()) == []


def test_tool_property_is_vscode_copilot(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert VSCodeCopilotExtractor().tool is Tool.VSCODE_COPILOT


def test_no_chat_sessions_dir_yields_nothing(monkeypatch, tmp_path):
    """A workspace dir without a chatSessions subdir is skipped."""
    ws = tmp_path / ".config" / "Code" / "User" / "workspaceStorage" / "ws-empty"
    ws.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(VSCodeCopilotExtractor().extract_sessions()) == []


# ---------------------------------------------------------------------------
# Legacy .json format
# ---------------------------------------------------------------------------


def test_parse_legacy_json_session(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    payload = {
        "customTitle": "Legacy chat",
        "requests": [
            {
                "requestId": "req-1",
                "message": {"text": "How do I sort a list?"},
                "response": [{"value": "Use the sorted() builtin."}],
            }
        ],
    }
    (sessions_dir / "legacy-session.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool is Tool.VSCODE_COPILOT
    assert s.session_id == "legacy-session"
    assert s.title == "Legacy chat"
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[0].content == "How do I sort a list?"
    assert s.messages[1].role is Role.ASSISTANT
    assert "sorted() builtin" in s.messages[1].content


def test_legacy_json_response_content_value_dict(monkeypatch, tmp_path):
    """Response items can carry a nested ``content.value`` markdown string."""
    sessions_dir = _chat_sessions_dir(tmp_path)
    payload = {
        "requests": [
            {
                "requestId": "req-1",
                "message": {"text": "explain decorators"},
                "response": [
                    {"content": {"value": "A decorator wraps a function."}},
                ],
            }
        ]
    }
    (sessions_dir / "nested.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assert any(
        "decorator wraps a function" in m.content
        for m in sessions[0].messages
        if m.role is Role.ASSISTANT
    )


def test_legacy_json_response_parts_fallback(monkeypatch, tmp_path):
    """When ``response`` is null, ``responseParts`` is used instead."""
    sessions_dir = _chat_sessions_dir(tmp_path)
    payload = {
        "requests": [
            {
                "requestId": "req-1",
                "message": {"text": "what is a generator"},
                "response": None,
                "responseParts": [{"value": "A generator yields lazily."}],
            }
        ]
    }
    (sessions_dir / "parts.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assert any("generator yields lazily" in m.content for m in sessions[0].messages)


def test_project_path_from_workspace_json(monkeypatch, tmp_path):
    _write_workspace_json(tmp_path, "ws-abc", "file:///home/user/myrepo")
    sessions_dir = _chat_sessions_dir(tmp_path, "ws-abc")
    payload = {
        "requests": [
            {
                "requestId": "req-1",
                "message": {"text": "a substantial prompt to pass thresholds"},
                "response": [{"value": "a substantial reply"}],
            }
        ]
    }
    (sessions_dir / "s.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].project_path == "/home/user/myrepo"


# ---------------------------------------------------------------------------
# New .jsonl format
# ---------------------------------------------------------------------------


def test_parse_jsonl_session(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    lines = [
        {
            "kind": 0,
            "v": {
                "creationDate": 1_700_000_000_000,
                "sessionId": "jsonl-sess-1",
                "customTitle": "JSONL chat",
            },
        },
        {
            "kind": 2,
            "v": [
                {
                    "requestId": "r1",
                    "message": {"text": "first user request"},
                    "timestamp": 1_700_000_010_000,
                }
            ],
        },
        {"kind": 2, "v": [{"value": "assistant chunk one"}]},
        {"kind": 2, "v": [{"value": "assistant chunk two"}]},
    ]
    (sessions_dir / "jsonl-sess-1.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "jsonl-sess-1"
    assert s.title == "JSONL chat"
    assert s.messages[0].role is Role.USER
    assert s.messages[0].content == "first user request"
    assert s.messages[1].role is Role.ASSISTANT
    # Chunks between requests are joined into a single assistant message.
    assert "assistant chunk one" in s.messages[1].content
    assert "assistant chunk two" in s.messages[1].content


def test_jsonl_thinking_parts_skipped(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    lines = [
        {"kind": 0, "v": {"sessionId": "thinking-sess", "creationDate": 1_700_000_000_000}},
        {
            "kind": 2,
            "v": [{"requestId": "r1", "message": {"text": "do the thing"}}],
        },
        {"kind": 2, "v": [{"kind": "thinking", "value": "internal reasoning"}]},
        {"kind": 2, "v": [{"value": "the visible answer"}]},
    ]
    (sessions_dir / "thinking-sess.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assistant = [m for m in sessions[0].messages if m.role is Role.ASSISTANT]
    assert len(assistant) == 1
    assert "the visible answer" in assistant[0].content
    assert "internal reasoning" not in assistant[0].content


def test_jsonl_two_requests_flush_assistant_between(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    lines = [
        {"kind": 0, "v": {"sessionId": "multi", "creationDate": 1_700_000_000_000}},
        {"kind": 2, "v": [{"requestId": "r1", "message": {"text": "question one"}}]},
        {"kind": 2, "v": [{"value": "answer one"}]},
        {"kind": 2, "v": [{"requestId": "r2", "message": {"text": "question two"}}]},
        {"kind": 2, "v": [{"value": "answer two"}]},
    ]
    (sessions_dir / "multi.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    roles = [m.role for m in sessions[0].messages]
    assert roles == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    assert sessions[0].messages[1].content == "answer one"
    assert sessions[0].messages[3].content == "answer two"


def test_jsonl_corrupt_lines_skipped(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    valid_header = json.dumps(
        {"kind": 0, "v": {"sessionId": "corrupt-sess", "creationDate": 1_700_000_000_000}}
    )
    valid_req = json.dumps(
        {"kind": 2, "v": [{"requestId": "r1", "message": {"text": "still works"}}]}
    )
    valid_resp = json.dumps({"kind": 2, "v": [{"value": "and the reply"}]})
    (sessions_dir / "corrupt-sess.jsonl").write_text(
        valid_header + "\nNOT JSON AT ALL\n" + valid_req + "\n{{bad\n" + valid_resp + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "corrupt-sess"
    assert sessions[0].message_count == 2


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_malformed_json_file_does_not_abort_run(monkeypatch, tmp_path):
    """A broken .json file is logged and skipped; siblings still parse."""
    sessions_dir = _chat_sessions_dir(tmp_path)
    (sessions_dir / "broken.json").write_text("not valid json", encoding="utf-8")
    good = {
        "requests": [
            {
                "requestId": "r1",
                "message": {"text": "good prompt that survives"},
                "response": [{"value": "good reply"}],
            }
        ]
    }
    (sessions_dir / "good.json").write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "good"


def test_extract_multiple_sessions(monkeypatch, tmp_path):
    sessions_dir = _chat_sessions_dir(tmp_path)
    for i in range(3):
        payload = {
            "requests": [
                {
                    "requestId": f"r{i}",
                    "message": {"text": f"prompt number {i}"},
                    "response": [{"value": f"reply number {i}"}],
                }
            ]
        }
        (sessions_dir / f"sess-{i}.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(VSCodeCopilotExtractor().extract_sessions())
    assert len(sessions) == 3
    assert {s.session_id for s in sessions} == {"sess-0", "sess-1", "sess-2"}


# ---------------------------------------------------------------------------
# Oversized-file guard (#96) — a runaway chat file must not spike RSS
# ---------------------------------------------------------------------------


def test_oversized_session_file_is_skipped_not_parsed(monkeypatch, tmp_path):
    """A session file above the size cap is skipped and tallied, not parsed.

    A single multi-hundred-MB chat file parsed whole via json.load spikes RSS
    ~10x its size (#96). The extractor caps it by file size before parsing.
    """
    sessions_dir = _chat_sessions_dir(tmp_path)

    # One normal session, one oversized (padded past the cap without actually
    # allocating hundreds of MB — the guard reads st_size, never the content).
    normal = {
        "requests": [
            {
                "requestId": "r0",
                "message": {"text": "a real prompt with enough content here"},
                "response": [{"value": "a real reply with enough content here"}],
            }
        ]
    }
    (sessions_dir / "small.json").write_text(json.dumps(normal), encoding="utf-8")

    ex = VSCodeCopilotExtractor()
    big = sessions_dir / "huge.json"
    # Write just over the cap; content is valid JSON but never reaches json.load.
    pad = " " * (ex.MAX_SESSION_FILE_BYTES + 1024)
    big.write_text(json.dumps(normal) + "\n//" + pad, encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    ex = VSCodeCopilotExtractor()
    sessions = list(ex.extract_sessions())

    ids = {s.session_id for s in sessions}
    assert "small" in ids
    assert "huge" not in ids
    assert ex.skip_counts.get("oversized_session_file") == 1
