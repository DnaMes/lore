"""Tests for Cline / Roo Code / Kilo Code extractors (issue #197 / B2.8a)."""

from __future__ import annotations

import builtins
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Role, Tool
from lore.extractors.cline import (
    PARSER_VERSION,
    ClineExtractor,
    KiloCodeExtractor,
    RooCodeExtractor,
)
from lore.extractors.factory import get_all_extractors
from lore.utils.tooling import normalize_tool_name

TASK_ID = "1740000000000"
CLINE_EXT = "saoudrizwan.claude-dev"
ROO_EXT = "rooveterinaryinc.roo-cline"
KILO_EXT = "kilocode.kilo-code"
WORKSPACE = "/work/sample-app"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _editor_tasks(home: Path, editor: str, ext_id: str, task_id: str = TASK_ID) -> Path:
    task_dir = home / ".config" / editor / "User" / "globalStorage" / ext_id / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _write_history(task_dir: Path, messages: list[dict[str, Any]]) -> Path:
    path = task_dir / "api_conversation_history.json"
    path.write_text(json.dumps(messages), encoding="utf-8")
    return path


def _write_ui(task_dir: Path, entries: list[dict[str, Any]]) -> None:
    (task_dir / "ui_messages.json").write_text(json.dumps(entries), encoding="utf-8")


def _basic_history() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi there"}],
        },
    ]


def _extract(
    cls: type = ClineExtractor,
    *,
    editor_roots: list[Path] | None = None,
    cli_root: Path | None = None,
) -> tuple[Any, list]:
    kwargs: dict[str, Any] = {}
    if editor_roots is not None:
        kwargs["editor_roots"] = editor_roots
    if cli_root is not None:
        kwargs["cli_sessions_root"] = cli_root
    extractor = cls(**kwargs)
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


def test_cline_basic_user_assistant(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    _write_history(task, _basic_history())
    _, sessions = _extract()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.CLINE
    assert session.session_id == TASK_ID
    assert session.origin == "interactive"
    roles = [m.role for m in session.messages]
    assert roles == [Role.USER, Role.ASSISTANT]
    assert session.messages[0].content == "hello"
    assert session.messages[1].content == "hi there"


def test_tool_use_and_results_completed_and_failed(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    history = [
        {"role": "user", "content": "run tools"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu-ok", "name": "read_file", "input": {"path": "/x"}},
                {"type": "tool_use", "id": "tu-bad", "name": "bash", "input": {"cmd": "false"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu-ok", "content": "file body"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-bad",
                    "content": "exit 1",
                    "is_error": True,
                },
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    _write_history(task, history)
    _, sessions = _extract()
    assert len(sessions) == 1
    msgs = sessions[0].messages
    assert msgs[1].content.startswith("[Tool: read_file]")
    events = [e for m in msgs for e in m.tool_events]
    assert len(events) == 2
    assert events[0]["status"] == "completed" and events[0]["is_error"] is False
    assert events[1]["status"] == "failed" and events[1]["is_error"] is True
    assert msgs[2].role == Role.TOOL
    assert "file body" in msgs[2].content


def test_thinking_maps_to_reasoning(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    history = [
        {"role": "user", "content": "think"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal chain"},
                {"type": "text", "text": "answer"},
            ],
        },
    ]
    _write_history(task, history)
    _, sessions = _extract()
    assistant = sessions[0].messages[1]
    assert assistant.reasoning == "internal chain"
    assert assistant.content == "answer"
    assert "internal" not in assistant.content


def test_environment_details_stripped_and_task_unwrapped(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    wrapped = (
        "<task>Do the thing</task>\n"
        "<environment_details>secret context</environment_details>\n"
        "extra"
    )
    history = [
        {"role": "user", "content": wrapped},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "yep"},
    ]
    _write_history(task, history)
    _, sessions = _extract()
    user_texts = [m.content for m in sessions[0].messages if m.role == Role.USER]
    assert user_texts[0] == "Do the thing\nextra"
    assert sessions[0].user_prompt_count == 3


def test_roo_history_item_title_and_workspace(home):
    task = _editor_tasks(home, "Code", ROO_EXT)
    _write_history(task, _basic_history())
    (task / "history_item.json").write_text(
        json.dumps(
            {
                "id": TASK_ID,
                "ts": 1740000000000,
                "task": "Roo native title",
                "workspace": WORKSPACE,
                "tokensIn": 1,
                "tokensOut": 2,
                "totalCost": 0.01,
            }
        ),
        encoding="utf-8",
    )
    _, sessions = _extract(RooCodeExtractor)
    session = sessions[0]
    assert session.title == "Roo native title"
    assert session.project_path == WORKSPACE


def test_roo_invalid_workspace_path_ignored(home):
    task = _editor_tasks(home, "Code", ROO_EXT)
    _write_history(task, _basic_history())
    (task / "history_item.json").write_text(
        json.dumps({"id": TASK_ID, "task": "t", "workspace": "/bad/../path"}),
        encoding="utf-8",
    )
    _, sessions = _extract(RooCodeExtractor)
    assert sessions[0].project_path is None


def test_kilo_reads_only_kilo_extension(home):
    kilo_task = _editor_tasks(home, "Code", KILO_EXT, task_id="kilo-1")
    _write_history(kilo_task, _basic_history())
    cline_task = _editor_tasks(home, "Code", CLINE_EXT, task_id="cline-1")
    _write_history(cline_task, _basic_history())
    _, kilo_sessions = _extract(KiloCodeExtractor)
    _, cline_sessions = _extract(ClineExtractor)
    assert {s.session_id for s in kilo_sessions} == {"kilo-1"}
    assert {s.session_id for s in cline_sessions} == {"cline-1"}


def test_ui_timestamps_when_counts_match(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    history = _basic_history()
    _write_history(task, history)
    _write_ui(
        task,
        [
            {"ts": 1_740_000_000_000, "type": "say", "say": "text", "text": "ignored title"},
            {"ts": 1_740_000_001_000, "type": "say", "say": "text", "text": "x"},
        ],
    )
    _, sessions = _extract()
    ts0 = sessions[0].messages[0].timestamp
    ts1 = sessions[0].messages[1].timestamp
    assert ts0 == datetime.fromtimestamp(1_740_000_000_000 / 1000)
    assert ts1 == datetime.fromtimestamp(1_740_000_001_000 / 1000)


def test_mtime_fallback_when_ui_count_mismatch(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    path = _write_history(task, _basic_history())
    _write_ui(task, [{"ts": 1_740_000_000_000, "type": "say", "say": "text", "text": "only one"}])
    fixed = 1_700_000_000.0
    os.utime(path, (fixed, fixed))
    _, sessions = _extract()
    expected = datetime.fromtimestamp(fixed)
    assert sessions[0].messages[0].timestamp == expected
    assert sessions[0].messages[1].timestamp == expected


def test_cli_session_list_form(home):
    cli_dir = home / ".cline" / "data" / "sessions"
    cli_dir.mkdir(parents=True)
    sid = "cli-session-1"
    (cli_dir / f"{sid}.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "cli user"},
                {"role": "assistant", "content": [{"type": "text", "text": "cli reply"}]},
            ]
        ),
        encoding="utf-8",
    )
    _, sessions = _extract(cli_root=cli_dir)
    assert len(sessions) == 1
    assert sessions[0].session_id == sid
    assert sessions[0].origin == "unknown"
    assert sessions[0].messages[0].content == "cli user"


def test_cli_session_messages_key_form(home):
    cli_dir = home / ".cline" / "data" / "sessions"
    cli_dir.mkdir(parents=True)
    sid = "cli-session-2"
    (cli_dir / f"{sid}.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": "from key"}]}),
        encoding="utf-8",
    )
    _, sessions = _extract(cli_root=cli_dir)
    assert sessions[0].messages[0].content == "from key"


def test_duplicate_task_id_qualified_independent_of_order(home):
    for editor in ("Code", "VSCodium"):
        task = _editor_tasks(home, editor, CLINE_EXT, task_id="dup-id")
        _write_history(task, _basic_history())
    roots = [
        home / ".config" / "Code" / "User" / "globalStorage" / CLINE_EXT / "tasks",
        home / ".config" / "VSCodium" / "User" / "globalStorage" / CLINE_EXT / "tasks",
    ]
    _, forward = _extract(editor_roots=roots)
    _, backward = _extract(editor_roots=list(reversed(roots)))
    expected = {"cline:Code:dup-id", "cline:VSCodium:dup-id"}
    assert {s.session_id for s in forward} == expected
    assert {s.session_id for s in backward} == expected


def test_forbidden_files_never_opened(home, monkeypatch):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    _write_history(task, _basic_history())
    for name in ("secrets", "settings.json", "cline_mcp_settings.json"):
        (task / name).write_text("blocked", encoding="utf-8")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file)
        for forbidden in ("secrets", "settings.json", "cline_mcp_settings.json", "checkpoints"):
            if forbidden in path:
                raise OSError(f"forbidden open: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_hostile_timestamp_and_types_do_not_abort(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    history = [
        {"role": "user", "content": "ok"},
        {"role": 123, "content": "bad role"},
        {"role": "assistant", "content": [{"type": "text", "text": "fine"}]},
    ]
    _write_history(task, history)
    _write_ui(
        task,
        [
            {"ts": 1e999, "type": "say", "say": "text", "text": "a"},
            {"ts": "not-a-number", "type": "say", "say": "text", "text": "b"},
        ],
    )
    _, sessions = _extract()
    assert len(sessions) == 1
    assert any(m.role == Role.ASSISTANT for m in sessions[0].messages)


def test_oversize_file_skipped(home, monkeypatch):
    import lore.extractors.cline as cline_mod

    monkeypatch.setattr(cline_mod, "_MAX_FILE_BYTES", 32)
    task = _editor_tasks(home, "Code", CLINE_EXT)
    _write_history(task, _basic_history())
    extractor, sessions = _extract()
    assert sessions == []
    assert extractor.record_counts.get("skipped_oversize", 0) >= 1


def test_symlinked_tasks_dir_skipped(home, tmp_path):
    outside = tmp_path / "outside"
    real_tasks = _editor_tasks(outside, "Code", CLINE_EXT)
    _write_history(real_tasks, _basic_history())
    link_parent = home / ".config" / "Code" / "User" / "globalStorage" / CLINE_EXT
    link_parent.mkdir(parents=True, exist_ok=True)
    (link_parent / "tasks").symlink_to(real_tasks.parent, target_is_directory=True)
    _, sessions = _extract()
    assert sessions == []


def test_symlinked_history_ignored(home, tmp_path):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    outside = tmp_path / "real.json"
    outside.write_text(json.dumps(_basic_history()), encoding="utf-8")
    (task / "api_conversation_history.json").symlink_to(outside)
    _, sessions = _extract()
    assert sessions == []


def test_message_cap_25000(home):
    task = _editor_tasks(home, "Code", CLINE_EXT)
    huge = [{"role": "user", "content": f"p{i}"} for i in range(25_000)]
    _write_history(task, huge)
    _, sessions = _extract()
    assert len(sessions) == 1
    assert len(sessions[0].messages) == 20_000


def test_normalize_cline_aliases():
    assert normalize_tool_name("cline") == "cline"
    assert normalize_tool_name("claude-dev") == "cline"
    assert normalize_tool_name("roo") == "roo-code"
    assert normalize_tool_name("roo-cline") == "roo-code"
    assert normalize_tool_name("kilo") == "kilo-code"
    assert normalize_tool_name("kilocode") == "kilo-code"


def test_extractors_registered():
    tools = {extractor.tool for extractor in get_all_extractors()}
    assert {Tool.CLINE, Tool.ROO_CODE, Tool.KILO_CODE} <= tools


def test_parser_version_is_one():
    assert PARSER_VERSION == 1
    assert ClineExtractor.PARSER_VERSION == 1


def test_secret_named_tool_inputs_are_not_rendered(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    task = tmp_path / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/111"
    task.mkdir(parents=True)
    history = [
        {"role": "user", "content": "<task>\nfetch\n</task>"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "http_get",
                    "input": {"url": "https://example.test", "api_key": "SENTINEL_KEY"},
                }
            ],
        },
    ]
    (task / "api_conversation_history.json").write_text(json.dumps(history), encoding="utf-8")

    sessions = list(ClineExtractor().extract_sessions())

    text = " ".join(m.content for s in sessions for m in s.messages)
    assert "https://example.test" in text
    assert "SENTINEL_KEY" not in text


def test_unclosed_environment_details_and_empty_wrapper_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    task = tmp_path / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/222"
    task.mkdir(parents=True)
    history = [
        {
            "role": "user",
            "content": "<task>\nreal prompt\n</task>\n<environment_details>\nSECRET_ENV",
        },
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "<environment_details>\nonly env\n</environment_details>"},
    ]
    (task / "api_conversation_history.json").write_text(json.dumps(history), encoding="utf-8")

    sessions = list(ClineExtractor().extract_sessions())

    users = [m.content for m in sessions[0].messages if m.role.value == "user"]
    assert users == ["real prompt"]


def test_oversized_title_and_content_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    task = tmp_path / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/333"
    task.mkdir(parents=True)
    history = [
        {"role": "user", "content": "p" * 300_000},
        {"role": "assistant", "content": "a" * 300_000},
    ]
    (task / "api_conversation_history.json").write_text(json.dumps(history), encoding="utf-8")
    ui = [
        {"ts": 1, "type": "say", "say": "task", "text": "T" * 50_000},
        {"ts": 2, "type": "say", "say": "text", "text": "x"},
    ]
    (task / "ui_messages.json").write_text(json.dumps(ui), encoding="utf-8")

    session = list(ClineExtractor().extract_sessions())[0]

    assert len(session.title) <= 200
    assert all(len(m.content) <= 100_000 for m in session.messages)
