"""Tests for the Kimi CLI extractor (issue #197 / B2.8d)."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.kimi import PARSER_VERSION, KimiExtractor, _md5_workdir
from lore.utils.tooling import normalize_tool_name

WORK_DIR = "/work/kimi-demo"
SESSION_ID = "kimi-sess-001"
SECRET_SENTINEL = "SENTINEL_NEVER_IN_OUTPUT"
SYSTEM_PROMPT_TEXT = "SECRET_SYSTEM_PROMPT_NEVER_STORED"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_jsonl(path: Path, lines: list[Any]) -> None:
    text = "\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _session_dir(home: Path, work_dir: str = WORK_DIR, session_id: str = SESSION_ID) -> Path:
    digest = _md5_workdir(work_dir)
    return home / ".kimi" / "sessions" / digest / session_id


def _registry(home: Path, work_dir: str = WORK_DIR) -> None:
    reg = home / ".kimi" / "kimi.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "work_dirs": [
                    {"path": work_dir, "kaos": "local", "last_session_id": SESSION_ID},
                ]
            }
        ),
        encoding="utf-8",
    )


def _basic_context(
    *,
    include_think: bool = False,
    tool_ok: bool = True,
    tool_fail: bool = False,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = [
        {"role": "_system_prompt", "content": SYSTEM_PROMPT_TEXT},
        {"role": "_checkpoint", "id": 1},
        {"role": "_usage", "token_count": 99},
        {"role": "user", "content": [{"type": "text", "text": "question one"}]},
    ]
    if include_think:
        lines.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "inner thought", "encrypted": "blob"},
                    {"type": "text", "text": "answer one"},
                ],
            }
        )
    else:
        lines.append({"role": "assistant", "content": [{"type": "text", "text": "answer one"}]})
    lines.append({"role": "user", "content": [{"type": "text", "text": "question two"}]})
    lines.append(
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "type": "function",
                    "id": "call-1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "/tmp/x", "api_key": SECRET_SENTINEL}),
                    },
                }
            ],
        }
    )
    if tool_ok:
        lines.append(
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": [{"type": "text", "text": "file body"}],
            }
        )
    if tool_fail:
        lines.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call-2",
                        "function": {"name": "run", "arguments": "{not-json"},
                    }
                ],
            }
        )
        lines.append(
            {
                "role": "tool",
                "tool_call_id": "call-2",
                "content": [{"type": "text", "text": "ERROR: boom"}],
            }
        )
    lines.append({"role": "user", "content": [{"type": "text", "text": "question three"}]})
    lines.append({"role": "assistant", "content": [{"type": "text", "text": "answer three"}]})
    lines.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": "CHECKPOINT 42"}],
        }
    )
    return lines


def _wire_small(turn_epochs: list[float]) -> list[dict[str, Any]]:
    lines = [{"type": "metadata", "protocol_version": 1}]
    for ts in turn_epochs:
        lines.append({"timestamp": ts, "message": {"type": "TurnBegin", "payload": {}}})
    return lines


def _setup_session(
    home: Path,
    *,
    work_dir: str = WORK_DIR,
    session_id: str = SESSION_ID,
    context: list[dict[str, Any]] | None = None,
    wire: list[dict[str, Any]] | None = None,
    title: str | None = "Native Kimi title",
) -> Path:
    _registry(home, work_dir)
    sdir = _session_dir(home, work_dir, session_id)
    ctx = context if context is not None else _basic_context(tool_ok=True, tool_fail=True)
    _write_jsonl(sdir / "context.jsonl", ctx)
    if wire is not None:
        _write_jsonl(sdir / "wire.jsonl", wire)
    else:
        _write_jsonl(
            sdir / "wire.jsonl", _wire_small([1_714_550_400.0, 1_714_550_500.0, 1_714_550_600.0])
        )
    if title is not None:
        (sdir / "state.json").write_text(
            json.dumps({"custom_title": title, "archived": False}), encoding="utf-8"
        )
    return sdir / "context.jsonl"


def _extract(extractor: KimiExtractor | None = None):
    extractor = extractor or KimiExtractor()
    return extractor, list(extractor.extract_sessions())


def test_kimi_basic_transcript(home):
    ctx = _basic_context(include_think=True, tool_ok=True, tool_fail=True)
    _setup_session(home, context=ctx)
    _, sessions = _extract()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.KIMI
    assert session.origin == "interactive"
    assert session.project_path == WORK_DIR
    assert session.title == "Native Kimi title"
    assert SECRET_SENTINEL not in " ".join(m.content for m in session.messages)
    assert SYSTEM_PROMPT_TEXT not in json.dumps(session.__dict__, default=str)
    users = [m for m in session.messages if m.role == Role.USER]
    assert len(users) == 3
    assistant = next(m for m in session.messages if "[Tool: read_file]" in m.content)
    assert assistant.tool_events[0]["status"] == "completed"
    failed = next(
        m for m in session.messages if m.tool_events and m.tool_events[0]["name"] == "run"
    )
    assert failed.tool_events[0]["status"] == "failed"
    think = next(m for m in session.messages if m.reasoning)
    assert "inner" in (think.reasoning or "")
    assert "encrypted" not in (think.reasoning or "").lower()


def test_kimi_md5_project_mapping(home):
    other = "/work/other-app"
    _setup_session(home, work_dir=other, session_id="other-sess")
    _, sessions = _extract()
    by_path = {s.project_path: s for s in sessions}
    assert by_path[other].session_id == "other-sess"


def test_kimi_wire_timestamps_on_users(home):
    _setup_session(
        home,
        wire=_wire_small([1_714_550_400.0, 1_714_550_410.0, 1_714_550_420.0]),
    )
    _, sessions = _extract()
    users = [m for m in sessions[0].messages if m.role == Role.USER]
    assert users[0].timestamp.year == 2024


def test_kimi_mtime_fallback_without_wire(home):
    path = _setup_session(home)
    (path.parent / "wire.jsonl").unlink()
    _, sessions = _extract()
    assert len(sessions) == 1


def test_kimi_duplicate_session_id_qualified(home):
    work_a = "/work/app-a"
    work_b = "/work/app-b"
    reg = home / ".kimi" / "kimi.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "work_dirs": [
                    {"path": work_a, "kaos": "local", "last_session_id": SESSION_ID},
                    {"path": work_b, "kaos": "local", "last_session_id": SESSION_ID},
                ]
            }
        ),
        encoding="utf-8",
    )
    for work_dir in (work_a, work_b):
        sdir = _session_dir(home, work_dir, SESSION_ID)
        _write_jsonl(sdir / "context.jsonl", _basic_context())
        _write_jsonl(sdir / "wire.jsonl", _wire_small([1_714_550_400.0] * 3))
    md5_a = _md5_workdir(work_a)
    md5_b = _md5_workdir(work_b)
    _, forward = _extract()
    ids_fwd = {s.session_id for s in forward}
    assert ids_fwd == {f"{md5_a}:{SESSION_ID}", f"{md5_b}:{SESSION_ID}"}
    _, backward = _extract()
    assert {s.session_id for s in backward} == ids_fwd


def test_kimi_hostile_input_does_not_abort(home, monkeypatch):
    import lore.extractors.kimi as kimi_mod

    _setup_session(home, context=_basic_context())
    path = _session_dir(home) / "context.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("x" * 9_000_000 + "\n")
    monkeypatch.setattr(kimi_mod, "_MAX_LINE_CHARS", 4096)
    nested = {"a": {"b": {"c": {"d": "deep"}}}}
    while len(json.dumps(nested)) < 5000:
        nested = {"k": nested}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(nested) + "\n")
    _, sessions = _extract()
    assert len(sessions) == 1


def test_kimi_symlink_session_skipped(home, tmp_path):
    _setup_session(home)
    real = _session_dir(home) / "context.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    real.unlink()
    real.symlink_to(outside)
    _, sessions = _extract()
    assert sessions == []


def test_kimi_symlink_sessions_root_not_followed(home, tmp_path):
    outside = tmp_path / "outside"
    digest = _md5_workdir(WORK_DIR)
    real = outside / ".kimi" / "sessions" / digest / SESSION_ID
    _write_jsonl(real / "context.jsonl", _basic_context())
    link = home / ".kimi" / "sessions"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / ".kimi" / "sessions", target_is_directory=True)
    _, sessions = _extract()
    assert sessions == []


def test_kimi_forbidden_neighbors(home, monkeypatch):
    _setup_session(home)
    kimi_home = home / ".kimi"
    for name in ("config.toml", "credentials", "mcp.json", "device_id"):
        (kimi_home / name).write_text("secret", encoding="utf-8")
    (kimi_home / "logs").mkdir(exist_ok=True)
    (kimi_home / "logs" / "app.log").write_text("log", encoding="utf-8")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file)
        for forbidden in (
            "config.toml",
            "credentials",
            "mcp.json",
            "device_id",
            "/logs/",
        ):
            if forbidden in path:
                raise OSError(f"forbidden: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    real_os_open = os.open

    def guarded_os_open(path, flags, mode=0o777):
        text = str(path)
        if any(x in text for x in ("config.toml", "credentials", "mcp.json", "device_id")):
            raise OSError(f"forbidden: {text}")
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(os, "open", guarded_os_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_normalize_kimi_aliases():
    assert normalize_tool_name("kimi") == "kimi"
    assert normalize_tool_name("kimi-cli") == "kimi"
    assert normalize_tool_name("kimi-code") == "kimi"


def test_kimi_registered():
    tools = {e.tool for e in get_all_extractors()}
    assert Tool.KIMI in tools


def test_kimi_parser_version():
    assert PARSER_VERSION == 1


def test_kimi_message_cap_stops_streaming_and_late_result_marks_failure(home, monkeypatch):
    import lore.extractors.kimi as kimi_mod

    monkeypatch.setattr(kimi_mod, "_MAX_MESSAGES", 4)
    ctx = [
        {"role": "user", "content": [{"type": "text", "text": "run it"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "calling"}],
            "tool_calls": [
                {"type": "function", "id": "c1", "function": {"name": "sh", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ERROR: boom"},
    ] + [{"role": "user", "content": [{"type": "text", "text": f"p{i}"}]} for i in range(50)]
    _setup_session(home, context=ctx)

    extractor, sessions = _extract()

    assert len(sessions[0].messages) == 4
    event = sessions[0].messages[1].tool_events[0]
    assert event["status"] == "failed" and event["is_error"] is True
    assert extractor.record_counts.get("message_limit") == 1


def test_kimi_symlinked_wire_is_not_read(home, tmp_path):
    ctx_path = _setup_session(home)
    wire = ctx_path.parent / "wire.jsonl"
    secret = tmp_path / "elsewhere.jsonl"
    secret.write_text(
        json.dumps({"timestamp": 1_000_000_000.0, "message": {"type": "TurnBegin"}}) + "\n",
        encoding="utf-8",
    )
    wire.unlink()
    wire.symlink_to(secret)

    _, sessions = _extract()

    assert sessions
    assert all(m.timestamp.year != 2001 for m in sessions[0].messages)
