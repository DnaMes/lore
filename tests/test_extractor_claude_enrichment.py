"""Claude Code extractor enrichment (task B2.6)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, TitleSource, Tool, UnifiedSession
from lore.extractors.claude import _MAX_REASONING_CHARS, ClaudeCodeExtractor
from lore.storage.reader import load_session_messages_v2
from lore.storage.writer import write_sessions


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _ts(t: str = "2025-06-15T10:00:00") -> str:
    return t


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / ".claude" / "projects" / "-home-user-app"
    project.mkdir(parents=True)
    return project


def _gate_user(i: int, sid: str, text: str = "user prompt") -> dict:
    return {
        "type": "user",
        "timestamp": _ts(f"2025-06-15T10:{i:02d}:00"),
        "sessionId": sid,
        "message": {"role": "user", "content": f"{text} {i}"},
        "uuid": f"u{i}",
    }


def _gate_assistant(i: int, sid: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": _ts(f"2025-06-15T10:{i:02d}:30"),
        "sessionId": sid,
        "message": {"role": "assistant", "content": f"reply {i}"},
        "uuid": f"a{i}",
    }


def _import_gate(sid: str = "sess-enrich") -> list[dict]:
    """Enough turns to pass default import thresholds."""
    records: list[dict] = []
    for n in range(3):
        records.append(_gate_user(n * 2, sid))
        records.append(_gate_assistant(n * 2 + 1, sid))
    return records


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


def _extract(home: Path, records: list[dict], sid: str = "sess-enrich") -> tuple:
    project = _make_project(home)
    _write_jsonl(project / f"{sid}.jsonl", records)
    extractor = ClaudeCodeExtractor()
    sessions = list(extractor.extract_sessions())
    assert len(sessions) == 1
    return extractor, sessions[0]


def test_parser_version_is_five():
    assert ClaudeCodeExtractor.PARSER_VERSION == 5


def test_title_precedence_custom_over_ai_over_legacy_summary(home):
    sid = "sess-titles"
    records = _import_gate(sid)
    records.insert(
        0,
        {"type": "summary", "summary": "Legacy summary title"},
    )
    records.insert(
        0,
        {"type": "ai-title", "aiTitle": "AI generated title", "sessionId": sid},
    )
    records.insert(
        0,
        {"type": "custom-title", "customTitle": "User custom title", "sessionId": sid},
    )
    _, session = _extract(home, records, sid)
    assert session.title == "User custom title"
    assert session.title_source == TitleSource.NATIVE
    assert session.summary == "Legacy summary title"


def test_title_ai_over_legacy_summary_fallback(home):
    sid = "sess-titles2"
    records = _import_gate(sid)
    records.insert(0, {"type": "summary", "summary": "Legacy only"})
    records.insert(0, {"type": "ai-title", "title": "AI via alias", "sessionId": sid})
    _, session = _extract(home, records, sid)
    assert session.title == "AI via alias"
    assert session.title_source == TitleSource.NATIVE


def test_thinking_goes_to_reasoning_not_content(home):
    sid = "sess-think"
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:00:00"),
            "sessionId": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal chain", "signature": "sig"},
                    {"type": "text", "text": "visible answer"},
                ],
            },
            "uuid": "a-think",
        }
    )
    _, session = _extract(home, records, sid)
    assistant = [m for m in session.messages if m.message_id == "a-think"]
    assert len(assistant) == 1
    msg = assistant[0]
    assert msg.reasoning == "internal chain"
    assert "internal chain" not in msg.content
    assert "visible answer" in msg.content


def test_redacted_thinking_ignored(home):
    sid = "sess-redacted"
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:00:00"),
            "sessionId": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking"},
                    {"type": "text", "text": "only text"},
                ],
            },
            "uuid": "a-red",
        }
    )
    _, session = _extract(home, records, sid)
    msg = next(m for m in session.messages if m.message_id == "a-red")
    assert msg.reasoning is None
    assert msg.content == "only text"


def test_assistant_message_model_set(home):
    sid = "sess-model"
    records = _import_gate(sid)
    records[-1] = {
        "type": "assistant",
        "timestamp": _ts("2025-06-15T11:00:00"),
        "sessionId": sid,
        "message": {"role": "assistant", "content": "ok", "model": "claude-sonnet-5-5"},
        "uuid": "a-model",
    }
    _, session = _extract(home, records, sid)
    msg = next(m for m in session.messages if m.message_id == "a-model")
    assert msg.model == "claude-sonnet-5-5"


def test_compact_summary_kind_and_not_user_prompt(home):
    sid = "sess-compact"
    records = _import_gate(sid)
    records.insert(
        1,
        {
            "type": "user",
            "timestamp": _ts("2025-06-15T09:30:00"),
            "sessionId": sid,
            "isCompactSummary": True,
            "message": {
                "role": "user",
                "content": "This session is being continued from a previous conversation.",
            },
            "uuid": "compact-1",
        },
    )
    _, session = _extract(home, records, sid)
    compact = next(m for m in session.messages if m.message_id == "compact-1")
    assert compact.role == Role.INFO
    assert compact.kind == "compaction_summary"
    assert session.user_prompt_count == 3


def test_is_meta_injected_context_not_user_prompt(home):
    sid = "sess-meta"
    records = _import_gate(sid)
    records.insert(
        1,
        {
            "type": "user",
            "timestamp": _ts("2025-06-15T09:20:00"),
            "sessionId": sid,
            "isMeta": True,
            "message": {"role": "user", "content": "injected system note"},
            "uuid": "meta-1",
        },
    )
    _, session = _extract(home, records, sid)
    meta = next(m for m in session.messages if m.message_id == "meta-1")
    assert meta.role == Role.USER
    assert meta.kind == "injected_context"
    assert session.user_prompt_count == 3


def test_compact_boundary_silently_ignored(home):
    sid = "sess-boundary"
    records = _import_gate(sid)
    records.insert(
        0,
        {"type": "system", "subtype": "compact_boundary", "timestamp": _ts("2025-06-15T09:00:00")},
    )
    extractor, session = _extract(home, records, sid)
    assert extractor.discovery["records_unknown"] == 0
    assert len(session.messages) == 6


def test_noise_record_types_not_unknown(home):
    sid = "sess-noise"
    records = _import_gate(sid)
    noise_types = [
        "attachment",
        "queue-operation",
        "file-history-snapshot",
        "last-prompt",
        "agent-name",
        "permission-mode",
        "progress",
    ]
    for n, rtype in enumerate(noise_types):
        records.insert(n, {"type": rtype, "timestamp": _ts("2025-06-15T08:00:00")})
    records.insert(0, {"type": "totally-made-up-type"})
    extractor, _ = _extract(home, records, sid)
    assert extractor.discovery["records_unknown"] == 1


def test_wrong_typed_fields_cost_only_that_field(home):
    sid = "sess-hostile"
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:00:00"),
            "sessionId": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": 5},
                    {"type": "text", "text": "still here"},
                ],
                "model": [],
            },
            "uuid": "a-bad",
        }
    )
    records.insert(
        0,
        {"type": "ai-title", "aiTitle": {}, "sessionId": sid},
    )
    records.insert(
        2,
        {
            "type": "user",
            "timestamp": _ts("2025-06-15T09:15:00"),
            "sessionId": sid,
            "isMeta": "yes",
            "message": {"role": "user", "content": "counts as prompt"},
            "uuid": "meta-str",
        },
    )
    _, session = _extract(home, records, sid)
    bad = next(m for m in session.messages if m.message_id == "a-bad")
    assert bad.reasoning is None
    assert bad.model is None
    assert "still here" in bad.content
    meta = next(m for m in session.messages if m.message_id == "meta-str")
    assert meta.kind == "text"
    assert session.title is not None


def test_thinking_truncated_at_cap(home):
    sid = "sess-long-think"
    records = _import_gate(sid)
    long_thinking = "z" * (_MAX_REASONING_CHARS + 500)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:00:00"),
            "sessionId": sid,
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": long_thinking}],
            },
            "uuid": "a-long",
        }
    )
    _, session = _extract(home, records, sid)
    msg = next(m for m in session.messages if m.message_id == "a-long")
    assert msg.reasoning is not None
    assert len(msg.reasoning) == _MAX_REASONING_CHARS


def test_subagent_metadata_unchanged_by_enrichment(home):
    sid = "parent1"
    project = _make_project(home)
    _write_jsonl(project / f"{sid}.jsonl", _import_gate(sid))
    sub_records = _import_gate("sub")
    sub_records.insert(
        0,
        {"type": "ai-title", "aiTitle": "Subagent native title", "sessionId": "sub"},
    )
    sub_path = project / sid / "subagents" / "agent-worker.jsonl"
    sub_path.parent.mkdir(parents=True)
    _write_jsonl(sub_path, sub_records)
    by_id = {s.session_id: s for s in ClaudeCodeExtractor().extract_sessions()}
    sub = by_id[f"{sid}:agent-worker"]
    assert sub.origin == "subagent"
    assert (sub.parent_session_id, sub.relation) == (sid, "subagent")
    assert sub.title.startswith("[subagent agent-worker]")


def test_unified_session_positional_construction_after_mtime_field_move():
    """Leading positional args stay stable when source_mtime_ns moves to the end."""
    t0 = datetime(2026, 1, 1)
    t1 = datetime(2026, 1, 2)
    session = UnifiedSession(
        Tool.CLAUDE_CODE,
        "sid-pos",
        t0,
        t1,
        [],
        "/proj",
        None,
        "thread-1",
        "Title",
    )
    assert session.session_id == "sid-pos"
    assert session.title == "Title"
    assert session.source_mtime_ns is None
    session.source_mtime_ns = 99
    assert session.source_mtime_ns == 99


def test_reasoning_is_capped_while_collecting_not_after_joining():
    """A huge thinking block is never joined in full: collection stops at the cap."""
    import tracemalloc

    from lore.extractors import claude as claude_mod

    big = "t" * 3_000_000
    blocks = [{"type": "thinking", "thinking": big}, {"type": "thinking", "thinking": big}]
    extractor = claude_mod.ClaudeCodeExtractor.__new__(claude_mod.ClaudeCodeExtractor)

    tracemalloc.start()
    try:
        _, _, reasoning = extractor._flatten_message_content(blocks, {}, collect_thinking=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert reasoning is not None
    assert len(reasoning) <= claude_mod._MAX_REASONING_CHARS
    assert peak < 1_000_000  # a join of both blocks would allocate about 6 MB


@pytest.mark.parametrize(
    "order",
    [
        ("custom", "ai", "legacy"),
        ("legacy", "ai", "custom"),
        ("ai", "legacy", "custom"),
        ("custom", "legacy", "ai"),
    ],
)
def test_title_precedence_does_not_depend_on_record_order(home, order):
    sid = "sess-order"
    title_records = {
        "custom": {"type": "custom-title", "customTitle": "Custom wins", "sessionId": sid},
        "ai": {"type": "ai-title", "aiTitle": "AI title", "sessionId": sid},
        "legacy": {"type": "summary", "summary": "Legacy summary", "leafUuid": "x"},
    }
    records = [title_records[name] for name in order] + _import_gate(sid)

    _, session = _extract(home, records, sid)

    assert session.title == "Custom wins"


# ---------------------------------------------------------------------------
# Canonical message envelope (#157)
# ---------------------------------------------------------------------------


def _envelope_records(sid: str) -> list[dict]:
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:00:00"),
            "sessionId": sid,
            "version": "2.1.0",
            "gitBranch": "sweep/extractors",
            "requestId": "req-1",
            "message": {
                "id": "msg-native-1",
                "role": "assistant",
                "model": "claude-sonnet-4",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 34,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 6,
                },
                "content": [
                    {"type": "thinking", "thinking": "internal chain", "signature": "sig-1"},
                    {
                        "type": "text",
                        "text": "Let me look.",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "Read",
                        "input": {"file_path": "/x/app.py"},
                    },
                ],
            },
            "uuid": "a-envelope",
        }
    )
    records.append(
        {
            "type": "user",
            "timestamp": _ts("2025-06-15T11:00:05"),
            "sessionId": sid,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "def main(): pass",
                        "is_error": False,
                    }
                ],
            },
            "uuid": "u-envelope",
        }
    )
    return records


def test_content_blocks_ids_usage_and_extensions_literal(home):
    sid = "sess-envelope"
    _, session = _extract(home, _envelope_records(sid), sid)
    assistant = next(m for m in session.messages if m.message_id == "a-envelope")

    expected_blocks = [
        {"type": "thinking", "thinking": "internal chain", "signature": "sig-1"},
        {"type": "text", "text": "Let me look."},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "Read",
            "input": {"file_path": "/x/app.py"},
        },
    ]
    assert assistant.content_blocks == expected_blocks
    assert assistant.reasoning == "internal chain"
    assert assistant.model == "claude-sonnet-4"
    assert assistant.message_id == "a-envelope"
    assert assistant.tokens == {
        "input": 12,
        "output": 34,
        "total": 46,
        "cache_creation": 5,
        "cache_read": 6,
    }
    assert assistant.extensions == {
        "claude": {
            "unknown_record_fields": {"requestId": "req-1"},
            "unknown_block_fields": [
                {
                    "index": 1,
                    "type": "text",
                    "fields": {"cache_control": {"type": "ephemeral"}},
                }
            ],
        }
    }
    assert assistant.tool_calls == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "Read",
            "input": {"file_path": "/x/app.py"},
            "output": "def main(): pass",
        }
    ]

    tool_result = next(m for m in session.messages if m.message_id == "u-envelope")
    assert tool_result.content_blocks == [
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": "def main(): pass",
            "is_error": False,
        }
    ]


def test_envelope_survives_v2_write_and_read(tmp_path, home):
    sid = "sess-roundtrip"
    _, session = _extract(home, _envelope_records(sid), sid)
    assistant = next(m for m in session.messages if m.message_id == "a-envelope")
    output_dir = tmp_path / "state"
    output_dir.mkdir(parents=True)
    write_sessions(output_dir / "index_v2.sqlite", [session])

    loaded = load_session_messages_v2(output_dir, session.session_id, include_reasoning=True)
    assert loaded is not None
    got = next(m for m in loaded if m.message_id == "a-envelope")
    assert got.content == assistant.content
    assert got.content_blocks == assistant.content_blocks
    assert got.tool_calls == assistant.tool_calls
    assert got.extensions == assistant.extensions
    assert got.reasoning == "internal chain"
    assert got.model == "claude-sonnet-4"
    assert got.tokens == assistant.tokens
    assert got.status == "unknown"


def test_image_and_document_blocks_are_descriptors_not_payloads(home):
    sid = "sess-media"
    image_bytes = b"png-bytes"
    document_bytes = b"%PDF-1.7"
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:05:00"),
            "sessionId": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": __import__("base64").b64encode(image_bytes).decode(),
                        },
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": __import__("base64").b64encode(document_bytes).decode(),
                        },
                    },
                    {"type": "provider_extension", "payload": {"keep": [1, 2]}},
                ],
            },
            "uuid": "a-media",
        }
    )
    credential = home / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text('{"token": "do-not-import"}', encoding="utf-8")

    _, session = _extract(home, records, sid)
    msg = next(m for m in session.messages if m.message_id == "a-media")

    assert msg.content_blocks == [
        {
            "type": "image",
            "media_type": "image/png",
            "source_type": "base64",
            "byte_length": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        },
        {
            "type": "document",
            "media_type": "application/pdf",
            "source_type": "base64",
            "byte_length": len(document_bytes),
            "sha256": hashlib.sha256(document_bytes).hexdigest(),
        },
        {"type": "provider_extension", "payload": {"keep": [1, 2]}},
    ]
    rendered = repr([(m.content, m.content_blocks, m.extensions) for m in session.messages])
    assert "png-bytes" not in rendered
    assert "do-not-import" not in rendered


def test_malformed_line_does_not_lose_later_records(home):
    sid = "sess-malformed"
    records = _import_gate(sid)
    records.append(
        {
            "type": "assistant",
            "timestamp": _ts("2025-06-15T11:10:00"),
            "sessionId": sid,
            "message": {"role": "assistant", "content": "after the bad line"},
            "uuid": "a-after",
        }
    )
    project = _make_project(home)
    lines = [json.dumps(record) for record in records]
    lines.insert(len(lines) - 1, "{not valid json")
    (project / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    session = list(ClaudeCodeExtractor().extract_sessions())[0]
    msg = next(m for m in session.messages if m.message_id == "a-after")
    assert msg.content == "after the bad line"
