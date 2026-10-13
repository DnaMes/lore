"""Tests for the Gemini CLI JSONL format (header, $set, $patch, $rewindTo, subagents)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from lore.core.models import Role, Tool
from lore.extractors.gemini import GeminiCLIExtractor

SESSION_ID = "11111111-aaaa-bbbb-cccc-000000000001"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_PROJECT_ROOTS", raising=False)
    return tmp_path


def _chats(home: Path, slug: str = "demo-project") -> Path:
    chats = home / ".gemini" / "tmp" / slug / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    return chats


def _write_lines(path: Path, records: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _header(session_id: str = SESSION_ID, **extra: Any) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "projectHash": "f" * 64,
        "startTime": "2026-05-01T10:00:00.000Z",
        "lastUpdated": "2026-05-01T10:00:00.000Z",
        "kind": "main",
        **extra,
    }


def _msg(msg_id: str, kind: str, content: Any, minute: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "id": msg_id,
        "timestamp": f"2026-05-01T10:{minute:02d}:00.000Z",
        "type": kind,
        "content": content,
        **extra,
    }


def _three_prompts() -> list[dict[str, Any]]:
    return [
        _msg("u1", "user", "first question", 0),
        _msg("g1", "gemini", "first answer", 1),
        _msg("u2", "user", "second question", 2),
        _msg("g2", "gemini", "second answer", 3),
        _msg("u3", "user", "third question", 4),
        _msg("g3", "gemini", "third answer", 5),
    ]


def _extract(extractor: GeminiCLIExtractor | None = None):
    extractor = extractor or GeminiCLIExtractor()
    return extractor, list(extractor.extract_sessions())


def test_set_only_file_is_replayed(home):
    _write_lines(
        _chats(home) / "session-2026-05-01T10-00-aaaa1111.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts(), "lastUpdated": "2026-05-01T10:30:00.000Z"}},
        ],
    )
    _, sessions = _extract()

    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool == Tool.GEMINI_CLI
    assert s.session_id == SESSION_ID
    assert s.message_count == 6
    assert [m.role for m in s.messages[:2]] == [Role.USER, Role.ASSISTANT]
    assert s.messages[0].content == "first question"
    assert s.last_updated.minute == 30
    assert s.project_hash == "f" * 64


def test_set_then_patch_updates_content_and_tool_calls(home):
    tool_call = {
        "id": "tc1",
        "name": "run_shell_command",
        "args": {"command": "ls -la"},
        "result": [{"functionResponse": {"response": {"output": "file.txt"}}}],
        "status": "success",
        "displayName": "Shell",
    }
    _write_lines(
        _chats(home) / "session-2026-05-01T10-00-aaaa1111.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts()}},
            {"$patch": {"id": "g1", "content": "patched answer"}},
            {"$patch": {"updates": [{"id": "g2", "toolCalls": [tool_call]}]}},
        ],
    )
    _, sessions = _extract()

    msgs = {m.message_id: m for m in sessions[0].messages}
    assert msgs["g1"].content == "patched answer"
    assert "second answer" in msgs["g2"].content
    assert "[Tool: run_shell_command]" in msgs["g2"].content
    assert msgs["g2"].tool_calls[0]["name"] == "run_shell_command"
    assert msgs["g2"].tool_calls[0]["status"] == "success"


def test_patch_remove_and_order_ids(home):
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts()}},
            {"$patch": {"removeIds": ["g3"]}},
            {"$patch": {"orderIds": ["u3", "u2", "u1"]}},
        ],
    )
    _, sessions = _extract()

    ids = [m.message_id for m in sessions[0].messages]
    assert "g3" not in ids
    assert ids[:3] == ["u3", "u2", "u1"]


def test_plain_message_records_last_write_wins(home):
    records: list[Any] = [_header()]
    records += _three_prompts()
    records.append(_msg("g1", "gemini", "rewritten answer", 1))
    _write_lines(_chats(home) / "session-x.jsonl", records)
    _, sessions = _extract()

    s = sessions[0]
    assert s.message_count == 6
    assert [m.content for m in s.messages if m.message_id == "g1"] == ["rewritten answer"]


def test_rewind_drops_message_and_tail(home):
    five = _three_prompts() + [_msg("u4", "user", "fourth", 6), _msg("g4", "gemini", "x", 7)]
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [_header(), {"$set": {"messages": five}}, {"$rewindTo": "g3"}],
    )
    _, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u1", "g1", "u2", "g2", "u3"]


def test_subagent_session_links_to_parent(home):
    chats = _chats(home)
    parent = "22222222-aaaa-bbbb-cccc-000000000002"
    _write_lines(
        chats / f"session-2026-05-01T10-00-{parent[:8]}.jsonl",
        [_header(parent), {"$set": {"messages": _three_prompts()}}],
    )
    sub_dir = chats / parent
    sub_dir.mkdir()
    _write_lines(
        sub_dir / "sub0001.jsonl",
        [_header("sub0001", kind="subagent"), {"$set": {"messages": _three_prompts()}}],
    )
    _, sessions = _extract()

    by_id = {s.session_id: s for s in sessions}
    assert set(by_id) == {parent, f"{parent}:sub0001"}
    sub = by_id[f"{parent}:sub0001"]
    assert sub.thread_id == f"gemini-cli:{parent}"
    assert sub.title is not None and sub.title.startswith("[subagent")
    assert by_id[parent].thread_id != sub.thread_id


def test_malformed_line_is_skipped_and_counted(home):
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts()[:2]}},
            "{this is not json",
            _msg("u2", "user", "second question", 2),
            _msg("u3", "user", "third question", 4),
        ],
    )
    extractor, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u1", "g1", "u2", "u3"]
    assert extractor.record_counts["malformed_lines"] == 1


def test_unknown_record_is_tolerated_and_counted(home):
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts()}},
            {"$futureOp": {"anything": 1}},
            {"mystery": True},
        ],
    )
    extractor, sessions = _extract()

    assert sessions[0].message_count == 6
    assert extractor.record_counts["unknown_records"] == 2


def test_project_root_file_resolves_path(home):
    chats = _chats(home, "my-slug")
    (chats.parent / ".project_root").write_text("/work/example-app\n", encoding="utf-8")
    _write_lines(chats / "session-x.jsonl", [_header(), {"$set": {"messages": _three_prompts()}}])
    _, sessions = _extract()

    assert sessions[0].project_path == "/work/example-app"
    assert sessions[0].thread_id is not None and sessions[0].thread_id.startswith("project:")


def test_projects_json_resolves_path_when_no_project_root(home):
    chats = _chats(home, "other-slug")
    (home / ".gemini" / "projects.json").write_text(
        json.dumps({"projects": {"/work/other-app": "other-slug"}}), encoding="utf-8"
    )
    _write_lines(chats / "session-x.jsonl", [_header(), {"$set": {"messages": _three_prompts()}}])
    _, sessions = _extract()

    assert sessions[0].project_path == "/work/other-app"


def test_legacy_json_still_parses_in_slug_dir(home):
    data = {
        "sessionId": "legacy-1",
        "startTime": "2025-06-15T10:00:00",
        "lastUpdated": "2025-06-15T10:30:00",
        "messages": [
            {"id": m["id"], "type": m["type"], "content": m["content"], "timestamp": m["timestamp"]}
            for m in _three_prompts()
        ],
    }
    (_chats(home) / "session-legacy.json").write_text(json.dumps(data), encoding="utf-8")
    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["legacy-1"]
    assert sessions[0].message_count == 6


def test_content_as_list_of_text_parts(home):
    messages = _three_prompts()
    messages[0]["content"] = [{"text": "multi "}, {"text": "part prompt"}]
    _write_lines(_chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": messages}}])
    _, sessions = _extract()

    assert sessions[0].messages[0].content == "multi part prompt"


def test_error_and_warning_map_to_info_role(home):
    messages = _three_prompts() + [_msg("e1", "error", "boom", 6), _msg("w1", "warning", "hm", 7)]
    _write_lines(_chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": messages}}])
    _, sessions = _extract()

    by_id = {m.message_id: m for m in sessions[0].messages}
    assert by_id["e1"].role == Role.INFO
    assert by_id["w1"].role == Role.INFO


def test_thoughts_tokens_and_model_are_kept(home):
    messages = _three_prompts()
    messages[1].update(
        thoughts=[{"subject": "Plan", "description": "Think first"}],
        tokens={"input": 10, "output": 5, "cached": 0, "thoughts": 2, "tool": 0, "total": 17},
        model="gemini-test-model",
    )
    _write_lines(_chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": messages}}])
    _, sessions = _extract()

    m = sessions[0].messages[1]
    assert m.model == "gemini-test-model"
    assert m.tokens is not None and m.tokens["total"] == 17
    assert m.reasoning is not None and "Plan" in m.reasoning


def test_session_below_quality_threshold_is_skipped(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [_header(), {"$set": {"messages": _three_prompts()[:4]}}],
    )
    extractor, sessions = _extract()

    assert sessions == []
    assert extractor.skip_counts["too_few_user_prompts"] == 1


def test_unchanged_source_is_not_reparsed(home):
    path = _write_lines(
        _chats(home) / "session-x.jsonl",
        [_header(), {"$set": {"messages": _three_prompts()}}],
    )
    extractor = GeminiCLIExtractor()
    extractor.known_source_mtimes = {str(path): path.stat().st_mtime_ns}

    assert list(extractor.extract_sessions()) == []
    assert str(path) in extractor.skipped_sources


# --- review fixes: hostile input, replay performance, wrong types, scoping ---


def _raw(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_overflow_timestamps_do_not_abort_the_run(home):
    bad = _chats(home, "a-bad")
    msg = '{"id":"u1","type":"user","content":"hi","timestamp":1e999}'
    _raw(
        bad / "session-x.jsonl",
        [
            '{"sessionId":"bad-1","startTime":1e300,"lastUpdated":1e999,"kind":"main"}',
            msg,
            '{"id":"u2","type":"user","content":"hi","timestamp":-1e999}',
            '{"id":"u3","type":"user","content":"hi","timestamp":1e300}',
            '{"$set":{"lastUpdated":1e999}}',
        ],
    )
    _write_lines(
        _chats(home, "b-good") / "session-y.jsonl",
        [_header(), {"$set": {"messages": _three_prompts()}}],
    )
    _, sessions = _extract()

    assert {s.session_id for s in sessions} == {"bad-1", SESSION_ID}
    bad_session = next(s for s in sessions if s.session_id == "bad-1")
    assert bad_session.message_count == 3
    assert bad_session.created_at.year > 1970


def test_replay_is_linear_for_plain_records_and_patches(home):
    n = 20_000
    records: list[Any] = [_header()]
    records += [
        _msg(f"m{i}", "user" if i % 2 == 0 else "gemini", f"c{i}", i % 60) for i in range(n)
    ]
    records += [{"$patch": {"id": f"m{i}", "content": f"p{i}"}} for i in range(5_000)]
    records.append(_msg("m10", "user", "rewritten", 0))
    _write_lines(_chats(home) / "session-x.jsonl", records)

    started = time.monotonic()
    _, sessions = _extract()
    elapsed = time.monotonic() - started

    assert elapsed < 10
    s = sessions[0]
    assert s.message_count == n
    assert [m.message_id for m in s.messages[:3]] == ["m0", "m1", "m2"]
    assert s.messages[10].content == "rewritten"
    assert s.messages[4_999].content == "p4999"
    assert s.messages[5_000].content == "c5000"


def test_many_order_and_remove_records_are_fast(home):
    n = 5_000
    records: list[Any] = [
        _header(),
        {"$set": {"messages": [_msg(f"m{i}", "user", "c") for i in range(n)]}},
    ]
    records += [{"$patch": {"orderIds": [f"m{i}"]}} for i in range(n)]
    records += [{"$patch": {"removeIds": [f"m{i}"]}} for i in range(0, n, 2)]
    _write_lines(_chats(home) / "session-x.jsonl", records)

    started = time.monotonic()
    _, sessions = _extract()
    elapsed = time.monotonic() - started

    assert elapsed < 10
    ids = [m.message_id for m in sessions[0].messages]
    assert len(ids) == n // 2
    assert ids[:3] == [f"m{n - 1 - 2 * k}" for k in range(3)]


def test_wrongly_typed_fields_cost_only_that_field(home):
    messages = _three_prompts()
    messages[1].update(toolCalls=5, thoughts=5, model=["x"])
    extractor_records: list[Any] = [
        _header(projectHash=5),
        {"$set": {"messages": messages}},
        _msg(["not", "a", "str"], "user", "list id"),
        {"$patch": {"id": ["x"], "content": "z"}},
        {"$patch": {"updates": [{"id": ["a"], "content": "z"}]}},
        {"$rewindTo": ["u1"]},
        {"$patch": {"removeIds": [["a"], "g3"], "orderIds": [["a"], "u3"]}},
    ]
    _write_lines(_chats(home) / "session-x.jsonl", extractor_records)
    extractor, sessions = _extract()

    assert len(sessions) == 1
    s = sessions[0]
    assert [m.message_id for m in s.messages] == ["u3", "u1", "g1", "u2", "g2"]
    assert s.messages[2].tool_calls == []
    assert s.messages[2].model is None
    assert s.project_hash is None
    assert extractor.record_counts["unknown_records"] == 4


def test_same_slug_in_two_homes_keeps_separate_projects(home):
    base_a = home / ".gemini" / "tmp"
    base_b = home / "other" / ".gemini" / "tmp"
    for base, project, sid in ((base_a, "/work/app-a", "sid-a"), (base_b, "/work/app-b", "sid-b")):
        chats = base / "shared-slug" / "chats"
        chats.mkdir(parents=True)
        (base.parent / "projects.json").write_text(
            json.dumps({"projects": {project: "shared-slug"}}), encoding="utf-8"
        )
        _write_lines(
            chats / "session-x.jsonl", [_header(sid), {"$set": {"messages": _three_prompts()}}]
        )
    extractor = GeminiCLIExtractor()
    extractor.base_paths = [base_a, base_b]
    _, sessions = _extract(extractor)

    by_id = {s.session_id: s for s in sessions}
    assert by_id["sid-a"].project_path == "/work/app-a"
    assert by_id["sid-b"].project_path == "/work/app-b"
    assert by_id["sid-a"].thread_id != by_id["sid-b"].thread_id


def test_nine_megabyte_line_is_counted_and_skipped(home):
    huge = json.dumps(_msg("big", "user", "x" * 9_000_000, 9))
    path = _chats(home) / "session-x.jsonl"
    _raw(path, [json.dumps(_header())] + [json.dumps(m) for m in _three_prompts()[:3]])
    with open(path, "a", encoding="utf-8") as f:
        f.write(huge + "\n")
        f.write(json.dumps(_msg("u9", "user", "after", 10)) + "\n")
    extractor, sessions = _extract()

    ids = [m.message_id for m in sessions[0].messages]
    assert ids == ["u1", "g1", "u2", "u9"]
    assert extractor.record_counts["malformed_lines"] == 1


def test_oversize_file_is_skipped_and_counted(home, monkeypatch):
    import lore.extractors.gemini as gemini_module

    monkeypatch.setattr(gemini_module, "_MAX_FILE_BYTES", 500)
    _write_lines(
        _chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": _three_prompts()}}]
    )
    extractor, sessions = _extract()

    assert sessions == []
    assert extractor.record_counts["skipped_oversize"] == 1


def test_symlinked_session_and_project_root_are_ignored(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = _write_lines(
        outside / "session-real.jsonl",
        [_header("linked"), {"$set": {"messages": _three_prompts()}}],
    )
    (outside / "root.txt").write_text("/secret/place\n", encoding="utf-8")
    chats = _chats(home)
    (chats / "session-link.jsonl").symlink_to(real)
    (chats.parent / ".project_root").symlink_to(outside / "root.txt")
    _write_lines(
        chats / "session-own.jsonl", [_header("own"), {"$set": {"messages": _three_prompts()}}]
    )
    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["own"]
    assert sessions[0].project_path is None


@pytest.mark.parametrize(
    "value",
    ["/a\n/b", "relative/path", "/work/../etc", "/work/a\x00b", "/" + "a" * 5000, "", "   "],
    ids=["newline", "relative", "dotdot", "nul", "overlong", "empty", "blank"],
)
def test_hostile_project_root_is_rejected(home, value):
    chats = _chats(home)
    (chats.parent / ".project_root").write_text(value, encoding="utf-8")
    _write_lines(chats / "session-x.jsonl", [_header(), {"$set": {"messages": _three_prompts()}}])
    _, sessions = _extract()

    assert len(sessions) == 1
    assert sessions[0].project_path is None


def test_deeply_nested_line_is_malformed_not_fatal(home):
    _raw(
        _chats(home) / "session-x.jsonl",
        [
            json.dumps(_header()),
            "[" * 200_000,
            json.dumps({"$set": {"messages": _three_prompts()}}),
        ],
    )
    extractor, sessions = _extract()

    assert sessions[0].message_count == 6
    assert extractor.record_counts["malformed_lines"] == 1


def test_content_as_single_part_object(home):
    messages = _three_prompts()
    messages[0]["content"] = {"text": "single part"}
    _write_lines(_chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": messages}}])
    _, sessions = _extract()

    assert sessions[0].messages[0].content == "single part"


def test_tool_call_arguments_and_output_are_capped(home):
    messages = _three_prompts()
    messages[1]["toolCalls"] = [
        {
            "id": "t1",
            "name": "custom_tool",
            "args": {"a": "x" * 500, "b": "y"},
            "result": "r" * 10_000,
        }
    ]
    _write_lines(_chats(home) / "session-x.jsonl", [_header(), {"$set": {"messages": messages}}])
    _, sessions = _extract()

    m = sessions[0].messages[1]
    assert "x" * 200 in m.content and "x" * 201 not in m.content
    output = m.tool_calls[0]["output"]
    assert output.startswith("r" * 4000) and output.endswith("[truncated]")
    assert len(output) < 4100


def test_duplicate_header_session_id_falls_back_to_file_stem(home):
    chats = _chats(home)
    for name in ("session-a", "session-b"):
        _write_lines(chats / f"{name}.jsonl", [_header(), {"$set": {"messages": _three_prompts()}}])
    _, sessions = _extract()

    assert sorted(s.session_id for s in sessions) == sorted([SESSION_ID, "session-b"])


def test_subagent_session_ids_are_stable_across_runs(home):
    chats = _chats(home)
    parent = "33333333-aaaa-bbbb-cccc-000000000003"
    _write_lines(
        chats / f"session-2026-05-01T10-00-{parent[:8]}.jsonl",
        [_header(parent), {"$set": {"messages": _three_prompts()}}],
    )
    sub_dir = chats / parent
    sub_dir.mkdir()
    _write_lines(
        sub_dir / "subA.jsonl",
        [_header(parent, kind="subagent"), {"$set": {"messages": _three_prompts()}}],
    )
    _write_lines(
        sub_dir / "subB.jsonl",
        [{"$set": {"kind": "subagent"}}, {"$set": {"messages": _three_prompts()}}],
    )
    _write_lines(
        sub_dir / "subC.jsonl",
        [_header(parent, kind="subagent"), {"$set": {"messages": _three_prompts()}}],
    )
    extractor, first = _extract()
    _, second = _extract(extractor)
    _, third = _extract()

    ids = [s.session_id for s in first]
    assert len(ids) == len(set(ids)) == 4
    assert ids == [s.session_id for s in second] == [s.session_id for s in third]
    assert f"{parent}:subB" in ids and f"{parent}:subC" in ids


def test_patch_merges_into_existing_tool_calls(home):
    messages = _three_prompts()
    messages[1]["toolCalls"] = [{"id": "tc1", "name": "read_file", "status": "pending"}]
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [
            _header(),
            {"$set": {"messages": messages}},
            {
                "$patch": {
                    "id": "g1",
                    "toolCalls": [
                        {"id": "tc1", "status": "success", "result": "done"},
                        {"id": "tc2", "name": "grep", "args": {"pattern": "x"}},
                    ],
                }
            },
        ],
    )
    _, sessions = _extract()

    calls = sessions[0].messages[1].tool_calls
    assert [c["id"] for c in calls] == ["tc1", "tc2"]
    assert calls[0]["name"] == "read_file"
    assert calls[0]["status"] == "success"
    assert calls[0]["output"] == "done"


def test_rewind_to_unknown_id_changes_nothing(home):
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [_header(), {"$set": {"messages": _three_prompts()}}, {"$rewindTo": "nope"}],
    )
    extractor, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages] == ["u1", "g1", "u2", "g2", "u3", "g3"]
    assert extractor.record_counts["unmatched_ids"] == 1


def test_rewritten_plain_record_keeps_its_position(home):
    records: list[Any] = [_header()] + _three_prompts()
    records.append(_msg("u1", "user", "rewritten first", 0))
    _write_lines(_chats(home) / "session-x.jsonl", records)
    _, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages][:2] == ["u1", "g1"]
    assert sessions[0].messages[0].content == "rewritten first"


def test_order_ids_with_duplicates_still_puts_listed_first(home):
    _write_lines(
        _chats(home) / "session-x.jsonl",
        [
            _header(),
            {"$set": {"messages": _three_prompts()}},
            {"$patch": {"orderIds": ["u3", "u3"]}},
        ],
    )
    _, sessions = _extract()

    assert [m.message_id for m in sessions[0].messages][:2] == ["u3", "u1"]
