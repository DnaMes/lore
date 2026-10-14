"""Tests for the Factory Droid extractor (issue #197 / B2.8e)."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.factory_droid import PARSER_VERSION, FactoryDroidExtractor
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SLUG = "demo-proj-slug"
CWD = "/work/factory-demo"
TOKEN_SENTINEL = "TOKEN_COUNTER_SENTINEL"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


def _session_path(home: Path) -> Path:
    return home / ".factory" / "sessions" / SLUG / f"{SESSION_ID}.jsonl"


def _settings_path(home: Path) -> Path:
    return home / ".factory" / "sessions" / SLUG / f"{SESSION_ID}.settings.json"


def _write_session(home: Path, lines: list[dict], *, settings: dict | None = None) -> Path:
    path = _session_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    if settings is not None:
        _settings_path(home).write_text(json.dumps(settings), encoding="utf-8")
    return path


def _basic_lines() -> list[dict]:
    return [
        {"type": "session_start", "title": "Factory title", "cwd": CWD},
        {
            "type": "message",
            "id": "m1",
            "timestamp": "2024-05-01T10:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        {
            "type": "message",
            "id": "m2",
            "timestamp": "2024-05-01T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "plan"},
                    {"type": "text", "text": "hi"},
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "read",
                        "input": {"path": "/x"},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": "m3",
            "timestamp": "2024-05-01T10:02:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "file body",
                        "is_error": False,
                    }
                ],
            },
        },
    ]


def _extract(extractor: FactoryDroidExtractor | None = None):
    extractor = extractor or FactoryDroidExtractor()
    return extractor, list(extractor.extract_sessions())


def test_factory_basic_transcript(home):
    _write_session(
        home, _basic_lines(), settings={"model": "factory-model", "title": "Settings title"}
    )
    _, sessions = _extract()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.FACTORY_DROID
    assert session.session_id == SESSION_ID
    assert session.project_path == CWD
    assert session.title == "Factory title"
    assistant = next(m for m in session.messages if m.role == Role.ASSISTANT)
    assert assistant.reasoning == "plan"
    assert "[Tool: read]" in assistant.content
    assert assistant.tool_events[0]["status"] == "completed"
    assert any(m.role == Role.TOOL for m in session.messages)


def test_factory_llm_only_injected_not_prompt(home):
    lines = _basic_lines()
    lines.append(
        {
            "type": "message",
            "id": "inj",
            "timestamp": "2024-05-01T10:03:00Z",
            "message": {
                "role": "user",
                "visibility": "llm_only",
                "content": [{"type": "text", "text": "hidden context"}],
            },
        }
    )
    _write_session(home, lines)
    _, sessions = _extract()
    assert sessions[0].user_prompt_count == 1
    assert any(m.kind == "injected_context" for m in sessions[0].messages)


def test_factory_user_only_skipped(home):
    lines = _basic_lines()
    lines.append(
        {
            "type": "message",
            "id": "uo",
            "timestamp": "2024-05-01T10:04:00Z",
            "message": {
                "role": "user",
                "visibility": "user_only",
                "content": [{"type": "text", "text": "ui only"}],
            },
        }
    )
    _write_session(home, lines)
    _, sessions = _extract()
    assert all("ui only" not in m.content for m in sessions[0].messages)


def test_factory_settings_model_and_extra_keys_ignored(home):
    _write_session(
        home,
        [
            {"type": "session_start", "cwd": CWD},
            {
                "type": "message",
                "id": "m0",
                "timestamp": "2024-05-01T09:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            },
        ],
        settings={
            "model": "from-settings",
            "title": "Settings only title",
            "total_tokens": TOKEN_SENTINEL,
            "input_token_count": TOKEN_SENTINEL,
        },
    )
    _, sessions = _extract()
    session = sessions[0]
    assert session.title == "Settings only title"
    dumped = json.dumps(session.__dict__, default=str)
    assert TOKEN_SENTINEL not in dumped
    assistants = [m for m in session.messages if m.role == Role.ASSISTANT]
    if assistants:
        assert assistants[0].model == "from-settings"


def test_factory_hostile_input(home, monkeypatch):
    import lore.extractors.factory_droid as fd_mod

    lines = _basic_lines()
    lines.append(
        {
            "type": "message",
            "id": "bad-ts",
            "timestamp": 1e999,
            "message": {"role": "user", "content": [{"type": "text", "text": "x"}]},
        }
    )
    path = _write_session(home, lines)
    nested = {"a": None}
    cur = nested
    for _ in range(200):
        cur["a"] = {"a": None}
        cur = cur["a"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "message": nested}) + "\n")
    monkeypatch.setattr(fd_mod, "_MAX_LINE_CHARS", 4096)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("y" * 9000 + "\n")
    _, sessions = _extract()
    assert len(sessions) == 1
    texts = [m.content for m in sessions[0].messages]
    assert "hello" in texts
    bad_ts = next(m for m in sessions[0].messages if m.content == "x")
    assert 1970 < bad_ts.timestamp.year < 2100  # bad timestamp falls back, record is kept
    assert not any(len(t) >= 4096 for t in texts)  # the oversized line was discarded


def test_factory_late_error_result_marks_event_failed(home):
    lines = [
        {"type": "session_start", "title": "t", "cwd": CWD},
        {
            "type": "message",
            "id": "a",
            "timestamp": "2024-05-01T10:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
        {
            "type": "message",
            "id": "b",
            "timestamp": "2024-05-01T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu9", "name": "sh", "input": {}}],
            },
        },
        {
            "type": "message",
            "id": "c",
            "timestamp": "2024-05-01T10:02:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu9", "content": "x", "is_error": True}
                ],
            },
        },
    ]
    _write_session(home, lines)

    _, sessions = _extract()

    event = next(m for m in sessions[0].messages if m.tool_events).tool_events[0]
    assert event["status"] == "failed" and event["is_error"] is True


def test_factory_symlink_skipped(home, tmp_path):
    _write_session(home, _basic_lines())
    path = _session_path(home)
    real = tmp_path / "real.jsonl"
    real.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(real)
    _, sessions = _extract()
    assert sessions == []


def test_factory_symlink_sessions_root_not_followed(home, tmp_path):
    outside = tmp_path / "outside"
    real = outside / ".factory" / "sessions" / SLUG / f"{SESSION_ID}.jsonl"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(
        "\n".join(json.dumps(line) for line in _basic_lines()) + "\n",
        encoding="utf-8",
    )
    link = home / ".factory" / "sessions"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / ".factory" / "sessions", target_is_directory=True)
    _, sessions = _extract()
    assert sessions == []


def test_factory_forbidden_neighbors(home, monkeypatch):
    _write_session(home, _basic_lines())
    factory_home = home / ".factory"
    for name in ("settings.json", "auth.json", "mcp.json"):
        (factory_home / name).write_text("secret", encoding="utf-8")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file)
        for forbidden in ("settings.json", "auth.json", "mcp.json"):
            if forbidden in path and not path.endswith(f"{SESSION_ID}.settings.json"):
                raise OSError(f"forbidden: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    real_os_open = os.open

    def guarded_os_open(path, flags, mode=0o777):
        text = str(path)
        if any(x in text for x in ("auth.json", "mcp.json")) or text.endswith("/settings.json"):
            raise OSError(f"forbidden: {text}")
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(os, "open", guarded_os_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_normalize_factory_aliases():
    assert normalize_tool_name("factory-droid") == "factory-droid"
    assert normalize_tool_name("droid") == "factory-droid"
    assert normalize_tool_name("factory") == "factory-droid"


def test_factory_registered():
    assert Tool.FACTORY_DROID in {e.tool for e in get_all_extractors()}


def test_factory_parser_version():
    assert PARSER_VERSION == 1
