"""Tests for the Command Code extractor (issue #197 / task B2.4)."""

from __future__ import annotations

import builtins
import json
import time
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Role, Tool
from lore.extractors.commandcode import CommandCodeExtractor
from lore.extractors.factory import get_all_extractors
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "00000000-aaaa-bbbb-cccc-000000000001"
CWD = "/work/example-app"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _slug(cwd: str = CWD) -> str:
    return cwd.strip("/").replace("/", "->")


def _projects_root(home: Path) -> Path:
    root = home / ".commandcode" / "projects" / _slug()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(path: Path, records: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _header(**extra: Any) -> dict[str, Any]:
    header: dict[str, Any] = {
        "type": "session",
        "version": 3,
        "id": SESSION_ID,
        "timestamp": "2026-05-01T10:00:00.000Z",
        "cwd": CWD,
    }
    header.update(extra)
    return header


def _v3_entry(rid: str, parent: Any, minute: int, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "message",
        "id": rid,
        "parentId": parent,
        "timestamp": f"2026-05-01T10:{minute:02d}:00.000Z",
    }
    entry.update(fields)
    return entry


def _v3_user(rid: str, parent: Any, minute: int, text: str) -> dict[str, Any]:
    return _v3_entry(
        rid,
        parent,
        minute,
        message={
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "meta": {"source": "user", "createdAt": "2026-05-01T10:00:00Z", "messageId": rid},
        },
    )


def _v3_assistant(
    rid: str,
    parent: Any,
    minute: int,
    blocks: list[dict[str, Any]],
    *,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = _v3_entry(
        rid,
        parent,
        minute,
        message={
            "role": "assistant",
            "content": blocks,
            "meta": {"source": "model", "createdAt": "2026-05-01T10:00:00Z", "messageId": rid},
        },
    )
    if model:
        entry["model"] = model
    if usage:
        entry["usage"] = usage
    return entry


def _tool_use(use_id: str = "tu-1", name: str = "read", **input_args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": use_id, "name": name, "input": input_args or {"path": "/x"}}


def _v3_tool_result(
    rid: str,
    parent: Any,
    minute: int,
    tool_use_id: str,
    text: str = "tool output",
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return _v3_entry(
        rid,
        parent,
        minute,
        message={
            "role": "user",
            "content": [block],
            "meta": {"source": "tool", "createdAt": "2026-05-01T10:00:00Z", "messageId": rid},
        },
    )


def _chain_v3(count: int = 3) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    parent: Any = None
    for index in range(count):
        prompt = _v3_user(f"u{index}", parent, index * 2, f"question {index}")
        answer = _v3_assistant(
            f"a{index}",
            prompt["id"],
            index * 2 + 1,
            [{"type": "text", "text": f"answer {index}"}],
        )
        entries.extend([prompt, answer])
        parent = answer["id"]
    return entries


def _transcript(
    home: Path,
    records: list[Any],
    name: str = f"{SESSION_ID}.jsonl",
    meta: dict[str, Any] | None = None,
) -> Path:
    root = _projects_root(home)
    path = _write(root / name, records)
    if meta is not None:
        (root / f"{SESSION_ID}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def _extract(extractor: CommandCodeExtractor | None = None):
    extractor = extractor or CommandCodeExtractor()
    return extractor, list(extractor.extract_sessions())


def test_v3_user_assistant_tool_use_result_usage_and_model(home):
    records: list[Any] = [_header()]
    records.append(_v3_user("u1", None, 0, "first question"))
    records.append(
        _v3_assistant(
            "a1",
            "u1",
            1,
            [{"type": "text", "text": "checking"}, _tool_use("tu-1", "read", path="/x")],
            model="provider/model-a",
            usage={
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadTokens": 2,
                "cacheWriteTokens": 1,
                "costUsd": 0.001,
            },
        )
    )
    records.append(_v3_tool_result("tr1", "a1", 2, "tu-1", "file contents"))
    records.append(_v3_user("u2", "tr1", 3, "second question"))
    records.append(_v3_assistant("a2", "u2", 4, [{"type": "text", "text": "answer two"}]))
    records.append(_v3_user("u3", "a2", 5, "third question"))
    records.append(_v3_assistant("a3", "u3", 6, [{"type": "text", "text": "answer three"}]))
    _transcript(home, records, meta={"entrypoint": "cli"})
    _, sessions = _extract()

    session = sessions[0]
    assert session.tool == Tool.COMMAND_CODE
    assert session.origin == "interactive"
    assert session.source_version == "3"
    assert session.project_path == CWD
    assistant = next(m for m in session.messages if m.message_id == "a1")
    assert assistant.model == "provider/model-a"
    assert assistant.tokens == {
        "input": 10,
        "output": 5,
        "cache_read": 2,
        "cache_write": 1,
        "total": 15,
    }
    assert "[Tool: read]" in assistant.content
    assert assistant.tool_events == [
        {"name": "read", "status": "completed", "duration_ms": None, "is_error": False}
    ]
    tool_msg = next(m for m in session.messages if m.message_id == "tr1")
    assert tool_msg.role == Role.TOOL
    assert tool_msg.content == "file contents"


def test_failed_tool_result_marks_event_failed(home):
    records: list[Any] = [_header()]
    records.append(_v3_user("u1", None, 0, "first question"))
    records.append(_v3_assistant("a1", "u1", 1, [_tool_use("tu-9", "bash", command="false")]))
    records.append(_v3_tool_result("tr1", "a1", 2, "tu-9", "boom", is_error=True))
    records.append(_v3_user("u2", "tr1", 3, "second question"))
    records.append(_v3_assistant("a2", "u2", 4, [{"type": "text", "text": "ok"}]))
    records.append(_v3_user("u3", "a2", 5, "third question"))
    _transcript(home, records)
    _, sessions = _extract()

    assistant = next(m for m in sessions[0].messages if m.message_id == "a1")
    assert assistant.tool_events[0]["status"] == "failed"
    assert assistant.tool_events[0]["is_error"] is True


def test_thinking_goes_to_reasoning_not_content(home):
    records: list[Any] = [_header()]
    records.append(_v3_user("u1", None, 0, "first question"))
    records.append(
        _v3_assistant(
            "a1",
            "u1",
            1,
            [
                {"type": "thinking", "thinking": "secret plan", "signature": "sig"},
                {"type": "text", "text": "visible answer"},
            ],
        )
    )
    records.append(_v3_user("u2", "a1", 2, "second question"))
    records.append(_v3_assistant("a2", "u2", 3, [{"type": "text", "text": "ok"}]))
    records.append(_v3_user("u3", "a2", 4, "third question"))
    _transcript(home, records)
    _, sessions = _extract()

    assistant = next(m for m in sessions[0].messages if m.message_id == "a1")
    assert assistant.reasoning == "secret plan"
    assert "secret plan" not in assistant.content
    assert assistant.content == "visible answer"


def test_flat_variant_with_meta_title(home):
    root = _projects_root(home)
    flat_id = "00000000-flat-flat-flat-flat00000001"
    records = [
        {
            "id": "m1",
            "timestamp": "2026-05-01T11:00:00.000Z",
            "sessionId": flat_id,
            "role": "user",
            "content": [{"type": "text", "text": "flat question one"}],
            "gitBranch": "main",
            "cwd": CWD,
        },
        {
            "id": "m2",
            "timestamp": "2026-05-01T11:01:00.000Z",
            "sessionId": flat_id,
            "role": "assistant",
            "content": [{"type": "text", "text": "flat answer"}],
            "gitBranch": "main",
        },
        {
            "id": "m3",
            "timestamp": "2026-05-01T11:02:00.000Z",
            "sessionId": flat_id,
            "role": "user",
            "content": [{"type": "text", "text": "flat question two"}],
            "gitBranch": "main",
        },
        {
            "id": "m4",
            "timestamp": "2026-05-01T11:03:00.000Z",
            "sessionId": flat_id,
            "role": "assistant",
            "content": [{"type": "text", "text": "flat answer two"}],
            "gitBranch": "main",
        },
        {
            "id": "m5",
            "timestamp": "2026-05-01T11:04:00.000Z",
            "sessionId": flat_id,
            "role": "user",
            "content": [{"type": "text", "text": "flat question three"}],
            "gitBranch": "main",
        },
    ]
    _write(root / f"{flat_id}.jsonl", records)
    (root / f"{flat_id}.meta.json").write_text(
        json.dumps(
            {
                "title": "Flat session title",
                "userRenamed": False,
                "projectPath": CWD,
                "cwd": CWD,
                "entrypoint": "cli",
            }
        ),
        encoding="utf-8",
    )
    _, sessions = _extract()

    assert len(sessions) == 1
    assert sessions[0].session_id == flat_id
    assert sessions[0].title == "Flat session title"
    assert sessions[0].project_path == CWD
    assert sessions[0].origin == "interactive"


def test_abandoned_branch_excluded(home):
    records: list[Any] = [_header(), _v3_user("u1", None, 0, "first question")]
    records.append(_v3_assistant("a1", "u1", 1, [{"type": "text", "text": "abandoned answer"}]))
    records.append(_v3_assistant("a2", "u1", 2, [{"type": "text", "text": "active answer"}]))
    records.append(_v3_user("u2", "a2", 3, "second question"))
    records.append(_v3_assistant("a3", "u2", 4, [{"type": "text", "text": "final answer"}]))
    _transcript(home, records)
    extractor, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u1", "a2", "u2", "a3"]
    assert extractor.record_counts.get("entries_off_branch", 0) == 1


def test_print_entrypoint_exec_bypasses_prompt_floor(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    root = _projects_root(home)
    cid = "00000000-print-print-print-print000001"
    lines = [
        json.dumps(
            {
                "id": "cp1",
                "messageId": "m1",
                "turnNumber": 1,
                "createdAt": "2026-05-01T12:00:00Z",
                "prompt": "only print prompt",
                "messageCount": 0,
                "files": [],
            }
        )
    ]
    (root / f"{cid}.checkpoints.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / f"{cid}.meta.json").write_text(
        json.dumps({"entrypoint": "print", "projectPath": CWD}),
        encoding="utf-8",
    )
    extractor, sessions = _extract()

    assert len(sessions) == 1
    assert sessions[0].origin == "exec"
    assert [m.content for m in sessions[0].messages] == ["only print prompt"]
    assert extractor.skip_counts.get("too_few_user_prompts", 0) == 0


def test_checkpoints_only_distinct_prompts_by_turn(home):
    root = _projects_root(home)
    cid = "00000000-chk-chk-chk-chk00000001"
    records = [
        {
            "id": "c1",
            "messageId": "m1",
            "turnNumber": 2,
            "createdAt": "2026-05-01T12:00:00Z",
            "prompt": "second prompt",
            "messageCount": 1,
            "files": [],
        },
        {
            "id": "c2",
            "messageId": "m2",
            "turnNumber": 1,
            "createdAt": "2026-05-01T11:00:00Z",
            "prompt": "first prompt",
            "messageCount": 1,
            "files": [],
        },
        {
            "id": "c3",
            "messageId": "m3",
            "turnNumber": 3,
            "createdAt": "2026-05-01T13:00:00Z",
            "prompt": "second prompt",
            "messageCount": 1,
            "files": [],
        },
        {
            "id": "c4",
            "messageId": "m4",
            "turnNumber": 4,
            "createdAt": "2026-05-01T14:00:00Z",
            "prompt": "third prompt",
            "messageCount": 1,
            "files": [],
        },
    ]
    _write(root / f"{cid}.checkpoints.jsonl", records)
    (root / f"{cid}.meta.json").write_text(json.dumps({"entrypoint": "cli"}), encoding="utf-8")
    _, sessions = _extract()

    # Turns are ordered by turnNumber; the same text in two distinct checkpoints is two
    # turns (a user may well repeat a prompt) and is kept.
    assert [m.content for m in sessions[0].messages] == [
        "first prompt",
        "second prompt",
        "second prompt",
        "third prompt",
    ]


def test_conversation_with_neither_transcript_nor_checkpoints_skipped(home):
    root = _projects_root(home)
    cid = "00000000-empty-empty-empty-empty01"
    (root / f"{cid}.meta.json").write_text(json.dumps({"entrypoint": "cli"}), encoding="utf-8")
    _, sessions = _extract()
    assert sessions == []


def test_auth_and_history_never_opened(home, monkeypatch):
    _transcript(home, [_header()] + _chain_v3(3))
    (home / ".commandcode" / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (home / ".commandcode" / "history.jsonl").write_text('{"p":"x","t":1}\n', encoding="utf-8")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = Path(file) if not isinstance(file, Path) else file
        name = path.name
        if name in {"auth.json", "history.jsonl"}:
            raise AssertionError(f"opened forbidden file: {name}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    real_path_open = Path.open

    def guarded_path_open(self, *args, **kwargs):
        if self.name in {"auth.json", "history.jsonl"}:
            raise AssertionError(f"opened forbidden file: {self.name}")
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_hostile_timestamp_and_ids_do_not_abort(home):
    raw = "\n".join(
        [
            json.dumps(_header(timestamp=1e999)),
            json.dumps(_v3_user("u1", None, 0, "first question")),
            json.dumps(_v3_assistant("a1", "u1", 1, [{"type": "text", "text": "ok"}])),
            json.dumps(_v3_user("u2", "a1", 2, "second question")),
            json.dumps(_v3_assistant("a2", "u2", 3, [{"type": "text", "text": "ok"}])),
            json.dumps(_v3_user("u3", "a2", 4, "third question")),
        ]
    )
    path = _projects_root(home) / "bad.jsonl"
    path.write_text(raw + "\n", encoding="utf-8")
    _, sessions = _extract()
    assert len(sessions) == 1
    assert sessions[0].created_at.year > 1970


def test_wrongly_typed_ids_are_ignored(home):
    records: list[Any] = [_header(id=5)]
    records.append(_v3_user("u1", None, 0, "first question"))
    records.append(_v3_user(["bad"], "u1", 1, "dropped"))
    records.append(_v3_user("u2", 42, 2, "second question"))
    records.append(_v3_assistant("a1", "u2", 3, [{"type": "text", "text": "ok"}]))
    _transcript(home, records, name="2026-05-01T10-00-00-000Z_stemcase.jsonl")
    _, sessions = _extract()
    assert sessions[0].session_id == "2026-05-01T10-00-00-000Z_stemcase"
    assert [m.message_id for m in sessions[0].messages] == ["u2", "a1"]


def test_oversize_line_skipped(home, monkeypatch):
    import lore.extractors.commandcode as cc_module

    monkeypatch.setattr(cc_module, "_MAX_LINE_CHARS", 4096)
    records: list[Any] = [_header()] + _chain_v3(3)[:4]
    path = _transcript(home, records)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_v3_user("ubig", "a1", 5, "y" * 9_000)) + "\n")
    extractor, sessions = _extract()
    assert extractor.record_counts["malformed_lines"] == 1
    assert "y" * 100 not in " ".join(m.content for m in sessions[0].messages)


def test_symlinked_projects_root_not_followed(home, tmp_path):
    outside = tmp_path / "outside"
    real_projects = outside / ".commandcode" / "projects" / _slug()
    real_projects.mkdir(parents=True, exist_ok=True)
    _write(real_projects / f"{SESSION_ID}.jsonl", [_header()] + _chain_v3(3))
    link = home / ".commandcode" / "projects"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / ".commandcode" / "projects", target_is_directory=True)
    _, sessions = _extract()
    assert sessions == []


def test_symlinked_transcript_ignored(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = _write(outside / "real.jsonl", [_header(id=SESSION_ID)] + _chain_v3(3))
    root = _projects_root(home)
    (root / "link.jsonl").symlink_to(real)
    _, sessions = _extract()
    assert sessions == []


def test_linear_time_for_large_session(home):
    parent: Any = None
    records: list[Any] = [_header()]
    for i in range(10_000):
        u = _v3_user(f"u{i:05d}", parent, i % 60, f"prompt {i}")
        a = _v3_assistant(f"a{i:05d}", u["id"], i % 60, [{"type": "text", "text": "ok"}])
        records.extend([u, a])
        parent = a["id"]
    _transcript(home, records)
    start = time.monotonic()
    _, sessions = _extract()
    elapsed = time.monotonic() - start
    assert len(sessions) == 1
    assert len(sessions[0].messages) == 20_000
    assert elapsed < 15.0


def test_normalize_command_code_aliases():
    assert normalize_tool_name("command-code") == "command-code"
    assert normalize_tool_name("commandcode") == "command-code"
    assert normalize_tool_name("cmd") == "command-code"


def test_command_code_extractor_registered():
    tools = {extractor.tool for extractor in get_all_extractors()}
    assert Tool.COMMAND_CODE in tools


def test_parser_version_is_one():
    assert CommandCodeExtractor.PARSER_VERSION == 1


# ---------------------------------------------------------------------------
# Review findings (B2.4)
# ---------------------------------------------------------------------------


def test_identical_prompts_in_distinct_turns_are_both_kept(home):
    rows = [
        {
            "id": "c1",
            "messageId": "m1",
            "turnNumber": 1,
            "createdAt": "2026-01-01T00:00:01Z",
            "prompt": "run the tests",
        },
        {
            "id": "c2",
            "messageId": "m2",
            "turnNumber": 2,
            "createdAt": "2026-01-01T00:00:02Z",
            "prompt": "run the tests",
        },
        {
            "id": "c2",
            "messageId": "m2",
            "turnNumber": 2,
            "createdAt": "2026-01-01T00:00:02Z",
            "prompt": "run the tests",
        },
    ]
    slug = home / ".commandcode" / "projects" / "home-example-app"
    slug.mkdir(parents=True)
    (slug / "turns.checkpoints.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (slug / "turns.meta.json").write_text(json.dumps({"entrypoint": "print"}))

    _, sessions = _extract()

    assert [m.content for m in sessions[0].messages] == ["run the tests", "run the tests"]


@pytest.mark.parametrize("turn", [1e999, float("nan"), "7", True, None])
def test_non_finite_turn_numbers_cost_only_that_row(home, turn):
    rows = [
        {"id": "c1", "turnNumber": 1, "createdAt": "2026-01-01T00:00:01Z", "prompt": "first"},
        {"id": "c2", "turnNumber": turn, "createdAt": "2026-01-01T00:00:02Z", "prompt": "second"},
    ]
    slug = home / ".commandcode" / "projects" / "home-example-app"
    slug.mkdir(parents=True)
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    (slug / "t.checkpoints.jsonl").write_text(text)
    (slug / "t.meta.json").write_text(json.dumps({"entrypoint": "print"}))

    _, sessions = _extract()

    assert len(sessions) == 1
    assert {m.content for m in sessions[0].messages} == {"first", "second"}


def test_string_false_is_error_is_not_a_failed_tool(home):
    slug = home / ".commandcode" / "projects" / "home-example-app"
    slug.mkdir(parents=True)
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "s-err",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/work/example-app",
        },
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}],
            },
        },
        {
            "type": "message",
            "id": "r1",
            "parentId": "a1",
            "timestamp": "2026-01-01T00:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "ok",
                        "is_error": "false",
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "u2",
            "parentId": "r1",
            "timestamp": "2026-01-01T00:00:04Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        },
        {
            "type": "message",
            "id": "a2",
            "parentId": "u2",
            "timestamp": "2026-01-01T00:00:05Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        },
    ]
    (slug / "s-err.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    _, sessions = _extract()

    events = [e for m in sessions[0].messages for e in m.tool_events]
    assert [e["status"] for e in events] == ["completed"]


def test_same_header_id_in_two_projects_gets_distinct_session_ids(home):
    for slug_name in ("home-example-a", "home-example-b"):
        slug = home / ".commandcode" / "projects" / slug_name
        slug.mkdir(parents=True)
        records = [
            {
                "type": "session",
                "version": 3,
                "id": "shared-id",
                "timestamp": "2026-01-01T00:00:00Z",
                "cwd": "/work/example",
            },
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "q1"}]},
            },
            {
                "type": "message",
                "id": "a1",
                "parentId": "u1",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            },
            {
                "type": "message",
                "id": "u2",
                "parentId": "a1",
                "timestamp": "2026-01-01T00:00:03Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "q2"}]},
            },
            {
                "type": "message",
                "id": "a2",
                "parentId": "u2",
                "timestamp": "2026-01-01T00:00:04Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
            },
        ]
        (slug / f"{slug_name}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    _, sessions = _extract()

    ids = [s.session_id for s in sessions]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_interactive_single_prompt_session_does_not_bypass_the_prompt_floor(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")  # conftest lowers it to 1
    slug = home / ".commandcode" / "projects" / "home-example-app"
    slug.mkdir(parents=True)
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "s-one",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/work/example-app",
        },
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "only prompt"}]},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        },
    ]
    (slug / "s-one.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    (slug / "s-one.meta.json").write_text(json.dumps({"entrypoint": "interactive"}))

    extractor, sessions = _extract()

    assert sessions == []
    assert extractor.skip_counts.get("too_few_user_prompts") == 1


def test_symlinked_slug_directory_and_sidecars_are_not_followed(home, tmp_path):
    outside = tmp_path / "outside-slug"
    outside.mkdir()
    rows = [{"id": "c1", "turnNumber": 1, "createdAt": "2026-01-01T00:00:01Z", "prompt": "p"}]
    (outside / "x.checkpoints.jsonl").write_text(json.dumps(rows[0]) + "\n")
    (outside / "x.meta.json").write_text(json.dumps({"entrypoint": "print"}))
    projects = home / ".commandcode" / "projects"
    projects.mkdir(parents=True)
    (projects / "linked-slug").symlink_to(outside, target_is_directory=True)

    _, sessions = _extract()

    assert sessions == []
