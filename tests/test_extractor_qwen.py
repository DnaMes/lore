"""Tests for the Qwen Code extractor (issue #197 / B2.8d)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.qwen import PARSER_VERSION, QwenExtractor
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CWD = "/work/qwen-demo"
SECRET_SENTINEL = "SENTINEL_NEVER_IN_OUTPUT"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _record(
    uuid: str,
    parent: str | None,
    rtype: str,
    *,
    subtype: str | None = None,
    minute: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION_ID,
        "timestamp": f"2026-06-01T10:{minute:02d}:00.000Z",
        "type": rtype,
        "cwd": CWD,
        "version": "0.4.1",
    }
    if subtype is not None:
        base["subtype"] = subtype
    base.update(extra)
    return base


def _user(uuid: str, parent: str | None, minute: int, text: str) -> dict[str, Any]:
    return _record(
        uuid,
        parent,
        "user",
        minute=minute,
        message={"role": "user", "parts": [{"text": text}]},
    )


def _assistant_tool(
    uuid: str,
    parent: str | None,
    minute: int,
    *,
    call_id: str = "fc-1",
    fail: bool = False,
) -> dict[str, Any]:
    return _record(
        uuid,
        parent,
        "assistant",
        minute=minute,
        model="qwen-model",
        usageMetadata={
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
        message={
            "role": "model",
            "parts": [
                {"text": "plan", "thought": True},
                {"text": "doing it"},
                {
                    "functionCall": {
                        "id": call_id,
                        "name": "grep",
                        "args": {"pattern": "x", "api_key": SECRET_SENTINEL},
                    }
                },
            ],
        },
    )


def _tool_result(
    uuid: str, parent: str, minute: int, call_id: str, text: str, *, error: bool
) -> dict[str, Any]:
    status = "error" if error else "success"
    response = {"error": text} if error else {"output": text}
    return _record(
        uuid,
        parent,
        "tool_result",
        minute=minute,
        toolCallResult={"status": status},
        message={
            "role": "model",
            "parts": [
                {
                    "functionResponse": {
                        "id": call_id,
                        "name": "grep",
                        "response": response,
                    }
                }
            ],
        },
    )


def _chain() -> list[dict[str, Any]]:
    return [
        _user("u1", None, 0, "question one"),
        _assistant_tool("a1", "u1", 1, call_id="fc-1"),
        _tool_result("t1", "a1", 2, "fc-1", "matches", error=False),
        _user("u2", "t1", 3, "question two"),
        _assistant_tool("a2", "u2", 4, call_id="fc-2", fail=True),
        _tool_result("t2", "a2", 5, "fc-2", "failed", error=True),
        _user("u3", "t2", 6, "question three"),
        _record(
            "a3",
            "u3",
            "assistant",
            minute=7,
            message={"role": "model", "parts": [{"text": "done"}]},
        ),
    ]


def _chat_path(home: Path, legacy: bool = False) -> Path:
    if legacy:
        root = home / ".qwen" / "tmp" / "proj-legacy" / "chats"
    else:
        root = home / ".qwen" / "projects" / "demo-slug" / "chats"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{SESSION_ID}.jsonl"


def _write_session(home: Path, records: list[dict[str, Any]], *, legacy: bool = False) -> Path:
    path = _chat_path(home, legacy=legacy)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _extract(extractor: QwenExtractor | None = None):
    extractor = extractor or QwenExtractor()
    return extractor, list(extractor.extract_sessions())


def test_qwen_basic_transcript(home):
    _write_session(home, _chain())
    _, sessions = _extract()
    session = sessions[0]
    assert session.tool == Tool.QWEN
    assert session.origin == "interactive"
    assert session.project_path == CWD
    assert session.source_version == "0.4.1"
    assert SECRET_SENTINEL not in " ".join(m.content for m in session.messages)
    assistant = next(m for m in session.messages if m.message_id == "a1")
    assert assistant.reasoning
    assert assistant.tool_events[0]["status"] == "completed"
    failed = next(m for m in session.messages if m.message_id == "a2")
    assert failed.tool_events[0]["status"] == "failed"


def test_qwen_branch_abandonment(home):
    records = _chain()[:4]
    records.extend(
        [
            _user("u-branch", "u1", 8, "abandoned branch"),
            _record(
                "a-branch",
                "u-branch",
                "assistant",
                minute=9,
                message={"role": "model", "parts": [{"text": "lost"}]},
            ),
        ]
    )
    records.extend(_chain()[4:])
    _write_session(home, records)
    _, sessions = _extract()
    assert not any(m.content == "lost" for m in sessions[0].messages)


def test_qwen_custom_title_and_compression(home):
    records = _chain()
    records[-1]["parentUuid"] = "sys-comp"
    records.extend(
        [
            _record(
                "sys-title",
                "u3",
                "system",
                minute=10,
                subtype="custom_title",
                systemPayload={"customTitle": "My Qwen chat"},
            ),
            _record(
                "sys-comp",
                "sys-title",
                "system",
                minute=11,
                subtype="chat_compression",
                systemPayload={
                    "compressedHistory": [
                        {"message": {"role": "user", "parts": [{"text": "old"}]}},
                        {"message": {"role": "model", "parts": [{"text": "summary text"}]}},
                    ]
                },
            ),
        ]
    )
    _write_session(home, records)
    _, sessions = _extract()
    session = sessions[0]
    assert session.title == "My Qwen chat"
    summary = next(m for m in session.messages if m.kind == "compaction_summary")
    assert summary.content == "summary text"


def test_qwen_parent_session_subagent(home):
    records = _chain()
    records[-1]["parentUuid"] = "sys-parent"
    records.append(
        _record(
            "sys-parent",
            "u3",
            "system",
            minute=12,
            subtype="parent_session",
            systemPayload={"parentSessionId": "parent-uuid-99"},
        )
    )
    _write_session(home, records)
    _, sessions = _extract()
    session = sessions[0]
    assert session.parent_session_id == "parent-uuid-99"
    assert session.relation == "subagent"
    assert session.origin == "subagent"


def test_qwen_injected_context_not_counted_as_prompt(home):
    records = _chain()
    records.append(
        _record(
            "inj",
            "a3",
            13,
            type="user",
            subtype="notification",
            message={"role": "user", "parts": [{"text": "system ping"}]},
        )
    )
    _write_session(home, records)
    _, sessions = _extract()
    assert sessions[0].user_prompt_count == 3
    assert any(m.kind == "injected_context" for m in sessions[0].messages)


def test_qwen_ledger_ignored(home):
    path = _chat_path(home)
    path.write_text("\n".join(json.dumps(r) for r in _chain()) + "\n", encoding="utf-8")
    ledger = path.with_name(f"{SESSION_ID}.ledger.jsonl")
    ledger.write_text(json.dumps(_user("x", None, 0, "ledger")) + "\n", encoding="utf-8")
    _, sessions = _extract()
    assert len(sessions) == 1


def test_qwen_legacy_tmp_root(home):
    _write_session(home, _chain(), legacy=True)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_qwen_hostile_and_oversize_line(home, monkeypatch):
    import lore.extractors.qwen as qwen_mod

    path = _write_session(home, _chain())
    monkeypatch.setattr(qwen_mod, "_MAX_LINE_CHARS", 4096)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user("big", "a3", 20, "z" * 9000)) + "\n")
    _, sessions = _extract()
    assert len(sessions) == 1


def test_qwen_symlink_file_skipped(home, tmp_path):
    path = _write_session(home, _chain())
    real = tmp_path / "real.jsonl"
    real.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(real)
    _, sessions = _extract()
    assert sessions == []


def test_normalize_qwen_aliases():
    assert normalize_tool_name("qwen") == "qwen"
    assert normalize_tool_name("qwen-code") == "qwen"
    assert normalize_tool_name("qwen-cli") == "qwen"


def test_qwen_registered():
    assert Tool.QWEN in {e.tool for e in get_all_extractors()}


def test_qwen_parser_version():
    assert PARSER_VERSION == 1


def test_qwen_record_limit_bounds_memory(home, monkeypatch):
    import lore.extractors.qwen as qwen_mod

    monkeypatch.setattr(qwen_mod, "_MAX_RECORDS", 3)
    records = [_user("u0", None, 0, "first")]
    for i in range(1, 10):
        records.append(_user(f"u{i}", f"u{i - 1}", i, f"prompt {i}"))
    _write_session(home, records)

    extractor, sessions = _extract()

    assert sessions and len(sessions[0].messages) <= 3
    assert extractor.record_counts.get("size_limit") == 1
