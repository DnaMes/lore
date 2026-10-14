"""Tests for Codex extractor content enrichment (task B2.2a)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedSession
from lore.extractors.codex import CodexExtractor


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _sessions_dir(home: Path) -> Path:
    d = home / ".codex" / "sessions" / "2025" / "06" / "15"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rollout(home: Path, records: list[dict], name: str = "rollout-enrich.jsonl") -> Path:
    path = _sessions_dir(home) / name
    _write_jsonl(path, records)
    return path


def _user_import_gate(ts: str = "2025-06-15T09:59:00", text: str = "fixture user prompt") -> dict:
    """One real user turn so relaxed import thresholds accept the session."""
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _meta(session_id: str = "sess-enrich-01", cwd: str | None = "/work/proj") -> dict:
    payload: dict = {"id": session_id, "cli_version": "9.9.9"}
    if cwd is not None:
        payload["cwd"] = cwd
    return {"type": "session_meta", "timestamp": "2025-06-15T10:00:00", "payload": payload}


def _extract(home: Path) -> tuple[CodexExtractor, list]:
    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_parser_version_is_three(home):
    assert CodexExtractor.PARSER_VERSION == 3


def test_function_call_and_output_completed(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": json.dumps({"path": "/tmp/foo.txt"}),
                "call_id": "call-read-1",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:05",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-read-1",
                "output": "file contents here",
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    s = sessions[0]
    tool_msgs = [m for m in s.messages if m.role == Role.TOOL]
    assert len(tool_msgs) == 1
    assert "file contents here" in tool_msgs[0].content
    assistant = [m for m in s.messages if m.role == Role.ASSISTANT][0]
    assert "[Tool: read_file]" in assistant.content
    assert assistant.tool_events == [
        {"name": "read_file", "status": "completed", "duration_ms": None, "is_error": False}
    ]


def test_function_call_output_failed_exit_code(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:00",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": json.dumps({"command": "false"}),
                "call_id": "call-sh-1",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:01:02",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-sh-1",
                "output": "command failed",
                "metadata": {"exit_code": 2},
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    assistant = [m for m in sessions[0].messages if m.role == Role.ASSISTANT][0]
    ev = assistant.tool_events[0]
    assert ev["status"] == "failed"
    assert ev["is_error"] is True


def test_custom_tool_call_and_output(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:02:00",
            "payload": {
                "type": "custom_tool_call",
                "name": "my_tool",
                "input": {"query": "search term"},
                "call_id": "custom-1",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:02:01",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "custom-1",
                "output": "custom result",
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    s = sessions[0]
    assert any(m.role == Role.TOOL and "custom result" in m.content for m in s.messages)
    assistant = [m for m in s.messages if m.role == Role.ASSISTANT][0]
    assert "[Tool: my_tool]" in assistant.content
    assert assistant.tool_events[0]["name"] == "my_tool"


def test_agent_message_info(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:03:00",
            "payload": {
                "type": "agent_message",
                "author": "reviewer",
                "recipient": "coder",
                "content": "Please fix the tests.",
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    info = [m for m in sessions[0].messages if m.role == Role.INFO]
    assert len(info) == 1
    assert info[0].content.startswith("reviewer:")
    assert "fix the tests" in info[0].content


def test_token_count_on_assistant_and_session_total(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:04:00",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "asst-1",
                "content": [{"type": "output_text", "text": "Hello world."}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2025-06-15T10:04:01",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cached_tokens": 2,
                        "reasoning_tokens": 1,
                        "total_tokens": 18,
                    },
                    "total_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 140,
                    },
                },
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    s = sessions[0]
    assistant = [m for m in s.messages if m.role == Role.ASSISTANT][0]
    assert assistant.tokens is not None
    assert assistant.tokens.get("input") == 10
    assert assistant.tokens.get("output") == 5
    assert assistant.tokens.get("total") == 18
    assert s.total_tokens == 140


def test_turn_context_sets_model_and_cwd_fallback(home):
    records = [
        _meta(cwd=None),
        _user_import_gate(),
        {
            "type": "turn_context",
            "timestamp": "2025-06-15T10:05:00",
            "payload": {
                "model": "gpt-test-model",
                "effort": "high",
                "cwd": "/work/from-turn",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:05:01",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    s = sessions[0]
    assert s.project_path == "/work/from-turn"
    assistant = [m for m in s.messages if m.role == Role.ASSISTANT][0]
    assert assistant.model == "gpt-test-model"


def test_compacted_compaction_summary(home):
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "compacted",
            "timestamp": "2025-06-15T10:06:00",
            "payload": {
                "message": "Earlier turns were summarized.",
                "replacement_history": [],
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    compacted = [m for m in sessions[0].messages if m.kind == "compaction_summary"]
    assert len(compacted) == 1
    assert compacted[0].role == Role.INFO
    assert "summarized" in compacted[0].content


def test_injected_context_not_counted_in_user_prompt_count(home):
    records = [
        _meta(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:07:00",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>\nOS: linux\n</environment_context>",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:07:30",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Real user question?"}],
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    s = sessions[0]
    kinds = [m.kind for m in s.messages if m.role == Role.USER]
    assert kinds == ["injected_context", "text"]
    assert s.user_prompt_count == 1


def test_unknown_record_type_counted_once(home):
    records = [
        _meta(),
        _user_import_gate(),
        {"type": "world_state", "timestamp": "2025-06-15T10:08:00", "payload": {}},
        {"type": "world_state", "timestamp": "2025-06-15T10:08:01", "payload": {}},
        {"type": "retained_context", "timestamp": "2025-06-15T10:08:02", "payload": {}},
    ]
    _rollout(home, records)
    extractor, sessions = _extract(home)
    assert len(sessions) == 1
    assert extractor.discovery["records_unknown"] == 2


def test_hostile_inputs_do_not_abort(home, monkeypatch):
    import lore.extractors.codex as codex_module

    monkeypatch.setattr(codex_module, "_MAX_LINE_CHARS", 4096)
    records = [
        _meta(),
        {
            "type": "response_item",
            "timestamp": 1e999,
            "payload": {
                "type": "function_call",
                "name": 123,
                "arguments": "{}",
                "call_id": 999,
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:09:00",
            "payload": {
                "type": "function_call_output",
                "call_id": ["bad"],
                "output": {"not": "a string"},
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:09:01",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "still here"}],
            },
        },
    ]
    path = _rollout(home, records)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("x" * 9_000 + "\n")
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert any(m.role == Role.USER for m in sessions[0].messages)


def test_linear_time_twenty_thousand_records(home):
    records = [_meta()]
    for i in range(10_000):
        records.append(
            {
                "type": "response_item",
                "timestamp": f"2025-06-15T10:{i % 60:02d}:00",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"prompt {i}"}],
                },
            }
        )
        records.append(
            {
                "type": "response_item",
                "timestamp": f"2025-06-15T10:{i % 60:02d}:01",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"reply {i}"}],
                },
            }
        )
    _rollout(home, records)
    start = time.monotonic()
    _, sessions = _extract(home)
    elapsed = time.monotonic() - start
    assert len(sessions) == 1
    assert len(sessions[0].messages) == 20_000
    assert elapsed < 20.0


def test_user_prompt_count_default_kind_unchanged():
    session = UnifiedSession(
        tool=Tool.CODEX,
        session_id="x",
        created_at=__import__("datetime").datetime(2025, 1, 1),
        last_updated=__import__("datetime").datetime(2025, 1, 1),
        messages=[
            __import__("lore.core.models", fromlist=["UnifiedMessage"]).UnifiedMessage(
                role=Role.USER,
                content="hello",
                timestamp=__import__("datetime").datetime(2025, 1, 1),
            ),
            __import__("lore.core.models", fromlist=["UnifiedMessage"]).UnifiedMessage(
                role=Role.USER,
                content="",
                timestamp=__import__("datetime").datetime(2025, 1, 1),
            ),
        ],
    )
    assert session.user_prompt_count == 1


def test_agents_md_block_is_injected_context(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    records = [
        _meta(),
        {
            "type": "message",
            "timestamp": "2025-06-15T10:10:00",
            "payload": {
                "role": "user",
                "content": (
                    "# AGENTS.md instructions for /work/proj\n\n"
                    "<INSTRUCTIONS>\nFollow these rules.\n</INSTRUCTIONS>"
                ),
                "id": "inj-1",
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    assert sessions[0].messages[0].kind == "injected_context"
    assert sessions[0].user_prompt_count == 0


# ---------------------------------------------------------------------------
# Review findings (B2.2a)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "# AGENTS.md should mention the new build step, can you add it?",
        "# AGENTS.md instructions for my repo are outdated, please review them",
        "<environment_context> shows the wrong shell, why?",
        "<user_instructions>do this</user_instructions> and then also explain why",
        "Please update <environment_context>",
    ],
)
def test_real_prompts_that_mention_injection_markers_stay_text(home, prompt):
    records = [
        _meta(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:07:30",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)

    assert [m.kind for m in sessions[0].messages if m.role == Role.USER] == ["text"]
    assert sessions[0].user_prompt_count == 1


def _call(call_id: str, name: str = "shell", ts: str = "2025-06-15T10:20:00") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps({"cmd": "ls"}),
            "call_id": call_id,
        },
    }


def _call_output(call_id: str, output: str = "ok", ts: str = "2025-06-15T10:20:05") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def test_duplicate_call_ids_are_matched_in_order(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    records = [
        _meta(),
        _call("dup", "first_tool", "2025-06-15T10:20:00"),
        _call("dup", "second_tool", "2025-06-15T10:20:01"),
        _call_output("dup", "one", "2025-06-15T10:20:02"),
        _call_output("dup", "two", "2025-06-15T10:20:03"),
    ]
    _rollout(home, records)
    _, sessions = _extract(home)

    events = [e for m in sessions[0].messages for e in m.tool_events]
    assert [(e["name"], e["status"]) for e in events] == [
        ("first_tool", "completed"),
        ("second_tool", "completed"),
    ]


def test_call_without_output_gets_an_explicit_status(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    records = [_meta(), _call("lost", "shell")]
    _rollout(home, records)
    _, sessions = _extract(home)

    events = [e for m in sessions[0].messages for e in m.tool_events]
    assert events == [
        {"name": "shell", "status": "no_result", "duration_ms": None, "is_error": False}
    ]


def test_tool_event_names_follow_their_own_call(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    records = [
        _meta(),
        _call("c1", "alpha", "2025-06-15T10:20:00"),
        _call("c2", "beta", "2025-06-15T10:20:01"),
        _call_output("c2", "b", "2025-06-15T10:20:02"),
        _call_output("c1", "a", "2025-06-15T10:20:03"),
    ]
    _rollout(home, records)
    _, sessions = _extract(home)

    events = [e for m in sessions[0].messages for e in m.tool_events]
    assert sorted(e["name"] for e in events) == ["alpha", "beta"]


def test_many_token_count_events_stay_linear(home, monkeypatch):
    import time

    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    records: list[dict] = [_meta()]
    for i in range(3000):
        records.append(
            {
                "type": "response_item",
                "timestamp": "2025-06-15T10:30:00",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"prompt {i}"}],
                },
            }
        )
    for i in range(6000):
        records.append(
            {
                "type": "event_msg",
                "timestamp": "2025-06-15T10:31:00",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 1, "total_tokens": 1}},
                },
            }
        )
    _rollout(home, records)

    start = time.monotonic()
    _, sessions = _extract(home)

    assert time.monotonic() - start < 15.0
    assert len(sessions) == 1


def test_custom_tool_call_string_input_is_rendered(home):
    long_input = "x" * 250
    records = [
        _meta(),
        _user_import_gate(),
        {
            "type": "response_item",
            "timestamp": "2025-06-15T10:02:00",
            "payload": {
                "type": "custom_tool_call",
                "name": "patch_tool",
                "input": long_input,
                "call_id": "call-str-1",
            },
        },
    ]
    _rollout(home, records)
    _, sessions = _extract(home)
    assistant = next(m for m in sessions[0].messages if m.role == Role.ASSISTANT)
    assert "x" * 200 in assistant.content
    assert "x" * 201 not in assistant.content
    assert "{}" not in assistant.content or "patch_tool" in assistant.content


def test_zst_skipped_line_bytes_count_toward_decode_limit(home, monkeypatch):
    import lore.extractors.codex as codex_module

    zstd = pytest.importorskip("zstandard")
    monkeypatch.setattr(codex_module, "_MAX_FILE_BYTES", 50_000)
    monkeypatch.setattr(codex_module, "_MAX_LINE_CHARS", 4096)
    huge = "z" * 40_000
    plain = (
        json.dumps(_meta(session_id="zst-cap"))
        + "\n"
        + json.dumps(_user_import_gate())
        + "\n"
        + json.dumps({"type": "oversize", "payload": huge})
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "timestamp": "2025-06-15T10:05:00",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "should not appear"}],
                },
            }
        )
        + "\n"
    )
    path = _sessions_dir(home) / "rollout-zst-limit.jsonl.zst"
    path.write_bytes(zstd.ZstdCompressor().compress(plain.encode("utf-8")))
    _, sessions = _extract(home)
    assert not any("should not appear" in (m.content or "") for s in sessions for m in s.messages)
