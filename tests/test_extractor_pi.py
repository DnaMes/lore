"""Tests for the Pi coding agent extractor (version 3 tree JSONL, issue #197 / B2.1).

Every fixture is a synthetic file written under a fake ``HOME`` in ``tmp_path``;
nothing here reads a real home directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.pi import PiExtractor
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "00000000-aaaa-bbbb-cccc-000000000001"
CWD = "/work/example-app"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _project_dir(root: Path, cwd: str = CWD) -> Path:
    slug = "--" + cwd.strip("/").replace("/", "-") + "--"
    target = root / slug
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write(path: Path, records: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _session_file(
    home: Path,
    records: list[Any],
    name: str = "2026-05-01T10-00-00-000Z_uuid.jsonl",
    cwd: str = CWD,
    root: str = ".pi/agent/sessions",
) -> Path:
    path = _project_dir(home / Path(root), cwd) / name
    return _write(path, records)


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


def _entry(rid: str, parent: Any, minute: int, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": rid,
        "parentId": parent,
        "timestamp": f"2026-05-01T10:{minute:02d}:00.000Z",
    }
    entry.update(fields)
    return entry


def _user(rid: str, parent: Any, minute: int, content: Any = "keep going") -> dict[str, Any]:
    return _entry(rid, parent, minute, type="message", message={"role": "user", "content": content})


def _assistant(
    rid: str, parent: Any, minute: int, blocks: Any = None, **fields: Any
) -> dict[str, Any]:
    if blocks is None:
        blocks = [{"type": "text", "text": "ok"}]
    message: dict[str, Any] = {"role": "assistant", "content": blocks}
    message.update(fields)
    return _entry(rid, parent, minute, type="message", message=message)


def _tool_result(
    rid: str,
    parent: Any,
    minute: int,
    call_id: str = "call-1",
    text: str = "result body",
    **fields: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "toolResult",
        "toolCallId": call_id,
        "toolName": "read",
        "content": [{"type": "text", "text": text}],
    }
    message.update(fields)
    return _entry(rid, parent, minute, type="message", message=message)


def _tool_call(call_id: str = "call-1", name: str = "read", **arguments: Any) -> dict[str, Any]:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": arguments or {"path": "/x"},
    }


def _chain(count: int = 3) -> list[dict[str, Any]]:
    """A linear branch: ``count`` user prompts each followed by an assistant answer."""
    entries: list[dict[str, Any]] = []
    parent: Any = None
    for index in range(count):
        prompt = _user(f"u{index}", parent, index * 2, f"question {index}")
        answer = _assistant(
            f"a{index}", prompt["id"], index * 2 + 1, [{"type": "text", "text": f"answer {index}"}]
        )
        entries.extend([prompt, answer])
        parent = answer["id"]
    return entries


def _extract(extractor: PiExtractor | None = None):
    extractor = extractor or PiExtractor()
    return extractor, list(extractor.extract_sessions())


def test_linear_session_messages_tools_and_usage(home):
    records: list[Any] = [_header()]
    records.append(_user("u1", None, 0, "first question"))
    records.append(
        _assistant(
            "a1",
            "u1",
            1,
            [{"type": "text", "text": "let me look"}, _tool_call("call-1", "read", path="/x")],
            model="pi-test-model",
            provider="testprovider",
            usage={
                "input": 100,
                "output": 40,
                "cacheRead": 7,
                "cacheWrite": 3,
                "totalTokens": 150,
                "cost": {"total": 0.01},
            },
            stopReason="toolUse",
            durationMs=1200,
        )
    )
    records.append(_tool_result("t1", "a1", 2, call_id="call-1", text="file contents"))
    records.append(_user("u2", "t1", 3, "second question"))
    records.append(_assistant("a2", "u2", 4))
    _session_file(home, records)
    extractor, sessions = _extract()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.PI
    assert session.session_id == SESSION_ID
    assert session.project_path == CWD
    assert session.source_version == "3"
    assert session.origin == "unknown"
    assert session.parser_version == extractor.PARSER_VERSION
    assert session.thread_id is not None and session.thread_id.startswith("project:")
    assert (session.created_at.month, session.created_at.day) == (5, 1)
    assert session.created_at.hour == 10 and session.created_at.minute == 0
    assert session.last_updated.minute == 4
    assert [m.message_id for m in session.messages] == ["u1", "a1", "t1", "u2", "a2"]
    assert [m.role for m in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.USER,
        Role.ASSISTANT,
    ]
    assert session.messages[0].content == "first question"
    assert session.messages[0].kind == "text"
    assert session.messages[1].content.startswith("let me look")
    assert "[Tool: read]" in session.messages[1].content
    assert session.messages[1].model == "pi-test-model"
    assert session.messages[1].tokens == {
        "input": 100,
        "output": 40,
        "cache_read": 7,
        "cache_write": 3,
        "total": 150,
    }
    assert session.total_tokens == 150
    assert session.messages[1].tool_events == [
        {"name": "read", "status": "completed", "duration_ms": None, "is_error": False}
    ]
    assert session.messages[2].content == "file contents"
    assert session.messages[1].parent_id == "u1"
    assert session.messages[1].active is True
    assert extractor.discovery["files_matched"] == 1
    assert extractor.discovery["files_parsed"] == 1
    assert extractor.record_counts.get("malformed_lines", 0) == 0


def test_active_branch_excludes_abandoned_sibling(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1, [{"type": "text", "text": "abandoned answer"}]))
    records.append(_assistant("a2", "u1", 2, [{"type": "text", "text": "active answer"}]))
    records.append(_user("u2", "a2", 3, "second question"))
    records.append(_assistant("a3", "u2", 4, [{"type": "text", "text": "final answer"}]))
    _session_file(home, records)
    extractor, sessions = _extract()

    session = sessions[0]
    assert [m.message_id for m in session.messages] == ["u1", "a2", "u2", "a3"]
    assert "abandoned answer" not in [m.content for m in session.messages]
    assert session.messages[1].content == "active answer"
    assert extractor.record_counts["entries_off_branch"] == 1


def test_compaction_and_branch_summary_are_typed_messages(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(
        _entry(
            "c1",
            "a1",
            2,
            type="compaction",
            summary="compact summary text",
            firstKeptEntryId="a1",
            tokensBefore=9000,
            details={"readFiles": [], "modifiedFiles": []},
        )
    )
    records.append(
        _entry("b1", "c1", 3, type="branch_summary", fromId="a1", summary="branch summary text")
    )
    records.append(_user("u2", "b1", 4, "second question"))
    records.append(_assistant("a2", "u2", 5))
    _session_file(home, records)
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["c1"].kind == "compaction_summary"
    assert by_id["c1"].role == Role.INFO
    assert by_id["c1"].content == "compact summary text"
    assert by_id["b1"].kind == "compaction_summary"
    assert by_id["b1"].content == "branch summary text"


def test_context_edit_with_null_replacement_deactivates_target(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_entry("e1", "u2", 3, type="context_edit", targetId="a1", replacement=None))
    records.append(_assistant("a2", "e1", 4))
    _session_file(home, records)
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["a1"].active is False
    assert by_id["u1"].active is True
    assert by_id["a2"].active is True


def test_custom_message_is_injected_context(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(
        _entry(
            "x1",
            "a1",
            2,
            type="custom_message",
            customType="subagent-notify",
            content="notice from elsewhere",
            display=True,
        )
    )
    records.append(_user("u2", "x1", 3, "second question"))
    records.append(_assistant("a2", "u2", 4))
    _session_file(home, records)
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["x1"].kind == "injected_context"
    assert by_id["x1"].role == Role.INFO
    assert by_id["x1"].content == "notice from elsewhere"


def test_session_info_names_the_session(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_entry("i1", "u1", 1, type="session_info", name="Refactor the parser"))
    records.append(_assistant("a1", "i1", 2))
    _session_file(home, records)
    _, sessions = _extract()

    assert sessions[0].title == "Refactor the parser"


def test_system_message_is_not_indexed(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(
        _entry(
            "s1",
            "u1",
            1,
            type="message",
            message={
                "role": "system",
                "content": "",
                "sections": {"preamble": "SECRET PREAMBLE", "tools": "tool docs"},
            },
        )
    )
    records.append(_assistant("a1", "s1", 2))
    _session_file(home, records)
    _, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u1", "a1"]
    assert "SECRET PREAMBLE" not in " ".join(m.content for m in sessions[0].messages)


def test_thinking_blocks_land_in_reasoning_not_content(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(
        _assistant(
            "a1",
            "u1",
            1,
            [
                {"type": "thinking", "thinking": "weigh the options", "thinkingSignature": "sig"},
                {"type": "text", "text": "the answer"},
            ],
        )
    )
    _session_file(home, records)
    _, sessions = _extract()

    message = sessions[0].messages[1]
    assert message.reasoning == "weigh the options"
    assert message.content == "the answer"


def test_failed_tool_result_marks_tool_event_failed(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1, [_tool_call("call-1", "bash", command="false")]))
    records.append(
        _tool_result("t1", "a1", 2, call_id="call-1", text="boom", isError=True, durationMs=77)
    )
    records.append(_user("u2", "t1", 3, "second question"))
    records.append(_assistant("a2", "u2", 4))
    _session_file(home, records)
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["a1"].tool_events == [
        {"name": "bash", "status": "failed", "duration_ms": 77, "is_error": True}
    ]
    assert by_id["t1"].role == Role.TOOL
    assert by_id["t1"].content == "boom"


def test_image_blocks_in_tool_result_become_placeholder(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1, [_tool_call("call-1", "read", path="/x")]))
    records.append(
        _entry(
            "t1",
            "a1",
            2,
            type="message",
            message={
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "read",
                "content": [
                    {"type": "text", "text": "top"},
                    {"type": "image", "data": "base64blob", "mimeType": "image/png"},
                ],
            },
        )
    )
    _session_file(home, records)
    _, sessions = _extract()

    content = sessions[0].messages[2].content
    assert content == "top\n[image]"
    assert "base64blob" not in content


def test_parent_session_header_creates_fork_relation(home):
    parent = "--work-example-app--/2026-04-01T09-00-00-000Z_deadbeef.jsonl"
    records: list[Any] = [_header(parentSession=parent)] + _chain()
    _session_file(home, records)
    _, sessions = _extract()

    session = sessions[0]
    assert session.parent_session_id == "2026-04-01T09-00-00-000Z_deadbeef"
    assert session.relation == "fork"


def test_sibling_root_omp_is_read(home):
    records: list[Any] = [_header(id="00000000-aaaa-bbbb-cccc-000000000002")] + _chain()
    _session_file(home, records, root=".omp/agent/sessions")
    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["00000000-aaaa-bbbb-cccc-000000000002"]
    assert sessions[0].tool == Tool.PI


def test_sibling_root_prime_is_read(home):
    records: list[Any] = [_header(id="00000000-aaaa-bbbb-cccc-000000000004")] + _chain()
    _session_file(home, records, root=".prime/agent/sessions")
    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["00000000-aaaa-bbbb-cccc-000000000004"]


def test_subagent_artifacts_and_other_files_are_skipped(home):
    records: list[Any] = [_header()] + _chain()
    _session_file(home, records)
    project = _project_dir(home / ".pi" / "agent" / "sessions")
    artifacts = project / "subagent-artifacts"
    artifacts.mkdir()
    _write(
        artifacts / "note.jsonl",
        [_header(id="00000000-aaaa-bbbb-cccc-000000000009")] + _chain(),
    )
    (project / "sidecar.txt").write_text("not a session\n", encoding="utf-8")
    extractor, sessions = _extract()

    assert [s.session_id for s in sessions] == [SESSION_ID]
    assert extractor.discovery["files_matched"] == 1


def test_tool_call_arguments_are_capped(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1, [_tool_call("call-1", "write", text="x" * 500)]))
    _session_file(home, records)
    _, sessions = _extract()

    content = sessions[0].messages[1].content
    assert "x" * 200 in content
    assert "x" * 201 not in content


def test_malformed_line_and_unknown_record_are_tolerated_and_counted(home):
    records: list[Any] = [
        _header(),
        "{this is not json",
        _user("u1", None, 0, "first question"),
        _entry("z1", "u1", 1, type="mystery_record", payload=1),
        _user("u2", "z1", 2, "second question"),
        _user("u3", "u2", 3, "third question"),
    ]
    _session_file(home, records)
    extractor, sessions = _extract()

    assert len(sessions) == 1
    assert [m.message_id for m in sessions[0].messages] == ["u1", "u2", "u3"]
    assert extractor.record_counts["malformed_lines"] == 1
    assert extractor.discovery["records_unknown"] == 1


def test_overflow_timestamp_falls_back_without_raising(home):
    raw = "\n".join(
        [
            json.dumps(_header(timestamp=1e999)),
            '{"id":"u1","parentId":null,"timestamp":1e999,"type":"message",'
            '"message":{"role":"user","content":"first question"}}',
            json.dumps(_assistant("a1", "u1", 1)),
            json.dumps(_user("u2", "a1", 2, "second question")),
            json.dumps(_assistant("a2", "u2", 3)),
        ]
    )
    path = _project_dir(home / ".pi" / "agent" / "sessions") / "bad.jsonl"
    path.write_text(raw + "\n", encoding="utf-8")
    _, sessions = _extract()

    assert len(sessions) == 1
    assert sessions[0].created_at.year > 1970
    assert sessions[0].last_updated.year > 1970


def test_wrongly_typed_ids_are_ignored(home):
    records: list[Any] = [_header(id=5), _user("u1", None, 0, "first question")]
    records.append(_user(["not", "a", "str"], "u1", 1, "id is a list, so this record is dropped"))
    records.append(_user("u2", 42, 2, "second question"))
    records.append(_assistant("a1", "u2", 3))
    _session_file(home, records)
    _, sessions = _extract()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == "2026-05-01T10-00-00-000Z_uuid"
    assert [m.message_id for m in session.messages] == ["u2", "a1"]


def test_oversized_line_is_counted_and_the_rest_of_the_file_is_read(home, monkeypatch):
    import lore.extractors.pi as pi_module

    monkeypatch.setattr(pi_module, "_MAX_LINE_CHARS", 4096)
    records: list[Any] = [_header()] + _chain()[:4]
    path = _session_file(home, records)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user("ubig", "a1", 5, "y" * 9_000)) + "\n")
        handle.write(json.dumps(_user("u9", "a1", 6, "after the huge line")) + "\n")
    extractor, sessions = _extract()

    assert extractor.record_counts["malformed_lines"] == 1
    ids = [m.message_id for m in sessions[0].messages]
    assert ids == ["u0", "a0", "u1", "a1", "u9"]
    assert "y" * 100 not in " ".join(m.content for m in sessions[0].messages)


def test_oversize_file_is_skipped_and_counted(home, monkeypatch):
    import lore.extractors.pi as pi_module

    monkeypatch.setattr(pi_module, "_MAX_FILE_BYTES", 500)
    _session_file(home, [_header()] + _chain())
    extractor, sessions = _extract()

    assert sessions == []
    assert extractor.record_counts["skipped_oversize"] == 1


def test_symlinked_session_is_ignored(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = _write(
        outside / "real.jsonl",
        [_header(id="00000000-aaaa-bbbb-cccc-000000000003")] + _chain(),
    )
    project = _project_dir(home / ".pi" / "agent" / "sessions")
    (project / "link.jsonl").symlink_to(real)
    _, sessions = _extract()

    assert sessions == []


def test_session_below_quality_threshold_is_skipped(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    _session_file(home, [_header()] + _chain(1))
    extractor, sessions = _extract()

    assert sessions == []
    assert extractor.skip_counts["too_few_user_prompts"] == 1


def test_missing_header_id_falls_back_to_file_stem(home):
    header = _header()
    header.pop("id")
    _session_file(home, [header] + _chain(), name="2026-05-01T10-00-00-000Z_stemcase.jsonl")
    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["2026-05-01T10-00-00-000Z_stemcase"]


def test_hostile_cwd_does_not_escape_the_archive(home):
    records: list[Any] = [_header(cwd="/work/../etc")] + _chain()
    _session_file(home, records)
    _, sessions = _extract()

    assert sessions[0].project_path is None


def test_unchanged_source_is_not_reparsed(home):
    path = _session_file(home, [_header()] + _chain())
    extractor = PiExtractor()
    extractor.known_source_mtimes = {str(path): path.stat().st_mtime_ns}

    assert list(extractor.extract_sessions()) == []
    assert str(path) in extractor.skipped_sources


def test_entries_before_the_header_are_still_read(home):
    records: list[Any] = [_user("u0", None, 0, "first question")]
    records.append(_header())
    records.append(_assistant("a0", "u0", 1))
    records.append(_user("u1", "a0", 2, "second question"))
    records.append(_assistant("a1", "u1", 3))
    records.append(_user("u2", "a1", 4, "third question"))
    _session_file(home, records)
    _, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u0", "a0", "u1", "a1", "u2"]


def test_cycle_in_parent_links_stops_without_hanging(home):
    records: list[Any] = [_header()] + _chain()
    records.append(_user("c1", "c2", 10, "cycle member one"))
    records.append(_assistant("c2", "c1", 11, [{"type": "text", "text": "cycle member two"}]))
    _session_file(home, records)
    _, sessions = _extract()

    assert sessions[0].messages[-1].message_id == "c2"


def test_linear_time_for_large_session(home):
    # 20 000 records completed in linear time within a generous bound
    parent: Any = None
    records: list[Any] = [_header()]
    for i in range(10_000):
        u = _user(f"u{i:05d}", parent, i % 60, f"prompt {i}")
        a = _assistant(f"a{i:05d}", f"u{i:05d}", i % 60)
        records.extend([u, a])
        parent = f"a{i:05d}"
    _session_file(home, records)

    start = time.monotonic()
    extractor, sessions = _extract()
    elapsed = time.monotonic() - start

    assert len(sessions) == 1
    assert len(sessions[0].messages) == 20_000
    assert elapsed < 15.0  # Generous bound for 20 000 records


def test_normalize_pi_coding_agent_alias():
    assert normalize_tool_name("pi-coding-agent") == "pi"
    assert normalize_tool_name("pi") == "pi"


def test_pi_extractor_is_registered():
    tools = {extractor.tool for extractor in get_all_extractors()}
    assert Tool.PI in tools


# ---------------------------------------------------------------------------
# Review findings (B2.1): terminal entries, wrongly typed fields, symlinked roots
# ---------------------------------------------------------------------------


def test_trailing_context_edit_still_deactivates_its_target(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_assistant("a2", "u2", 3))
    records.append(_entry("e1", "a2", 4, type="context_edit", targetId="a1", replacement=None))
    _session_file(home, records)
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["a1"].active is False
    assert by_id["a2"].active is True


def test_trailing_session_info_still_sets_the_title(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_assistant("a2", "u2", 3))
    records.append(_entry("i1", "a2", 4, type="session_info", name="Named at the end"))
    _session_file(home, records)
    _, sessions = _extract()

    assert sessions[0].title == "Named at the end"


@pytest.mark.parametrize("target", [["a1"], {"id": "a1"}, 5])
def test_non_string_context_edit_target_costs_only_that_record(home, target):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_entry("e1", "u2", 3, type="context_edit", targetId=target, replacement=None))
    records.append(_assistant("a2", "e1", 4))
    _session_file(home, records)
    _, sessions = _extract()

    assert len(sessions) == 1
    assert {m.message_id for m in sessions[0].messages} >= {"u1", "a1", "u2", "a2"}


def test_non_string_tool_result_text_does_not_drop_the_session(home):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(
        _assistant(
            "a1", "u1", 1, [{"type": "toolCall", "id": "t1", "name": "bash", "arguments": {}}]
        )
    )
    records.append(
        _entry(
            "r1",
            "a1",
            2,
            type="message",
            message={
                "role": "toolResult",
                "toolCallId": "t1",
                "content": [{"type": "text", "text": ["not", "a", "string"]}, {"type": "text"}],
            },
        )
    )
    records.append(_user("u2", "r1", 3, "second question"))
    records.append(_assistant("a2", "u2", 4))
    _session_file(home, records)
    _, sessions = _extract()

    assert len(sessions) == 1


@pytest.mark.parametrize("call_id", [["t1"], {"x": 1}, 7, None])
def test_non_string_tool_call_id_does_not_drop_the_session(home, call_id):
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(
        _assistant(
            "a1", "u1", 1, [{"type": "toolCall", "id": call_id, "name": "bash", "arguments": {}}]
        )
    )
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_assistant("a2", "u2", 3))
    _session_file(home, records)
    _, sessions = _extract()

    assert len(sessions) == 1


def test_symlinked_sessions_root_is_not_followed(home, tmp_path):
    outside = tmp_path / "outside"
    project = _project_dir(outside)
    records: list[Any] = [_header(), _user("u1", None, 0, "first question")]
    records.append(_assistant("a1", "u1", 1))
    records.append(_user("u2", "a1", 2, "second question"))
    records.append(_assistant("a2", "u2", 3))
    (project / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    link = home / ".pi" / "agent" / "sessions"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    _, sessions = _extract()

    assert sessions == []
