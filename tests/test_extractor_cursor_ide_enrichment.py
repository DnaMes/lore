"""Tests for Cursor IDE state.vscdb enrichment (task B2.7b)."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from lore.extractors.cursor import CursorExtractor

_SENTINEL_BLOB = "SENTINEL_BLOB_KEY_VALUE_XX"
_SENTINEL_SPEC = "SENTINEL_SPEC_KEY_VALUE_XX"
_SENTINEL_API = "SENTINEL_API_TOKEN_VALUE_XX"


def _db_path(home: Path) -> Path:
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _make_db(
    home: Path,
    rows: dict[str, str],
    headers: list[tuple] | None = None,
) -> Path:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in rows.items():
            conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, value))
        if headers is not None:
            conn.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER)"
            )
            conn.executemany("INSERT INTO composerHeaders VALUES (?,?,?,?,?)", headers)
        conn.commit()
    finally:
        conn.close()
    return path


def _composer(
    composer_id: str,
    *,
    name: str = "Enriched chat",
    headers: list[dict] | None = None,
    bubbles: dict[str, dict] | None = None,
    extra: dict | None = None,
) -> dict[str, str]:
    composer: dict = {
        "name": name,
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_050_000,
    }
    if headers is not None:
        composer["fullConversationHeadersOnly"] = headers
    if extra:
        composer.update(extra)
    rows = {f"composerData:{composer_id}": json.dumps(composer)}
    if bubbles:
        for bid, payload in bubbles.items():
            rows[f"bubbleId:{composer_id}:{bid}"] = json.dumps(payload)
    return rows


def _sessions(home: Path) -> list:
    return list(CursorExtractor().extract_sessions())


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


def test_parser_version_is_three():
    assert CursorExtractor.PARSER_VERSION == 3


def test_bubble_timestamp_and_model(home):
    rows = _composer(
        "c-ts",
        headers=[
            {"bubbleId": "b1", "type": 2},
        ],
        bubbles={
            "b1": {
                "text": "answer",
                "createdAt": "2024-06-01T12:00:00",
                "modelInfo": {"modelName": "gpt-test-model"},
            }
        },
    )
    _make_db(home, rows)
    s = _sessions(home)[0]
    msg = s.messages[0]
    assert msg.model == "gpt-test-model"
    assert msg.timestamp.year == 2024
    assert msg.timestamp.month == 6


def test_thinking_goes_to_reasoning_not_content(home):
    rows = _composer(
        "c-think",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "visible answer",
                "thinking": {"text": "hidden chain"},
                "allThinkingBlocks": [{"text": "block two"}],
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert "hidden chain" in (msg.reasoning or "")
    assert "block two" in (msg.reasoning or "")
    assert "hidden chain" not in msg.content
    assert "block two" not in msg.content


def test_tool_completed_inline_and_event(home):
    rows = _composer(
        "c-tool-ok",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "done",
                "toolFormerData": {
                    "name": "read_file",
                    "status": "completed",
                    "params": {"path": "/tmp/x"},
                    "result": {"ok": True},
                    "startedAtMs": 1_000,
                    "completedAtMs": 1_500,
                },
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert "[Tool: read_file]" in msg.content
    assert "path=/tmp/x" in msg.content
    assert msg.tool_events == [
        {"name": "read_file", "status": "completed", "duration_ms": 500, "is_error": False}
    ]
    dumped = json.dumps(msg.tool_events)
    assert "ok" not in dumped


def test_tool_failed_event(home):
    rows = _composer(
        "c-tool-fail",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "oops",
                "toolFormerData": {
                    "name": "shell",
                    "status": "error",
                    "params": {},
                },
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert msg.tool_events[0]["status"] == "failed"
    assert msg.tool_events[0]["is_error"] is True


def test_token_count_on_message(home):
    rows = _composer(
        "c-tok",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "tok",
                "tokenCount": {"inputTokens": 10, "outputTokens": 5},
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert msg.tokens == {"input": 10, "output": 5, "total": 15}


def test_composer_headers_override_session_times(home):
    rows = _composer("c-hdr", headers=[{"bubbleId": "b1", "type": 1}])
    rows["bubbleId:c-hdr:b1"] = json.dumps({"text": "hi"})
    _make_db(
        home,
        rows,
        headers=[("c-hdr", 1_600_000_000_000, 1_600_000_099_000, 0, 0)],
    )
    s = _sessions(home)[0]
    assert int(s.created_at.timestamp() * 1000) == 1_600_000_000_000
    assert int(s.last_updated.timestamp() * 1000) == 1_600_000_099_000


def test_composer_headers_is_subagent_sets_origin(home):
    rows = _composer(
        "c-sub",
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "sub work"}},
    )
    _make_db(home, rows, headers=[("c-sub", 1_700_000_000_000, 1_700_000_000_000, 0, 1)])
    s = _sessions(home)[0]
    assert s.origin == "subagent"


def test_subagent_composer_ids_parent_child(home):
    parent_rows = _composer(
        "parent-1",
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "spawn"}},
        extra={"subagentComposerIds": ["child-1"]},
    )
    child_rows = _composer(
        "child-1",
        headers=[{"bubbleId": "b2", "type": 2}],
        bubbles={"b2": {"text": "child reply"}},
    )
    merged = {**parent_rows, **child_rows}
    _make_db(home, merged)
    sessions = {s.session_id: s for s in _sessions(home)}
    child = sessions["child-1"]
    assert child.parent_session_id == "parent-1"
    assert child.relation == "subagent"
    assert child.origin == "subagent"


def test_missing_composer_headers_table_tolerated(home):
    rows = _composer(
        "c-nohdr",
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "still works"}},
    )
    _make_db(home, rows)
    assert len(_sessions(home)) == 1


def test_corrupt_db_skipped_without_raising(home):
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite file")
    assert _sessions(home) == []


def test_original_db_unchanged_no_wal_shm(home):
    rows = _composer(
        "c-copy",
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "copy safe"}},
    )
    path = _make_db(home, rows)
    before = path.read_bytes()
    list(CursorExtractor().extract_sessions())
    after = path.read_bytes()
    assert before == after
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_hostile_bubble_values_do_not_raise(home):
    rows = _composer(
        "c-hostile",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "x" * 120_000,
                "createdAt": float("inf"),
                "thinking": math.nan,
                "modelInfo": "not-a-dict",
                "tokenCount": {"inputTokens": "nope", "outputTokens": 1},
                "toolFormerData": "bad",
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert len(msg.content) <= 100_000


def test_secret_fields_never_in_session_serialization(home):
    rows = _composer(
        "c-secret",
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "safe prompt"}},
        extra={
            "blobEncryptionKey": _SENTINEL_BLOB,
            "speculativeSummarizationEncryptionKey": _SENTINEL_SPEC,
            "apiToken": _SENTINEL_API,
        },
    )
    _make_db(home, rows)
    sessions = _sessions(home)
    blob = " ".join(repr(s) + json.dumps(s, default=str) for s in sessions)
    for sentinel in (_SENTINEL_BLOB, _SENTINEL_SPEC, _SENTINEL_API):
        assert sentinel not in blob


def test_started_at_ms_fallback_timestamp(home):
    rows = _composer(
        "c-fallback",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "t",
                "startedAtMs": 1_700_000_010_000,
            }
        },
    )
    _make_db(home, rows)
    msg = _sessions(home)[0].messages[0]
    assert int(msg.timestamp.timestamp() * 1000) == 1_700_000_010_000


def test_secret_named_tool_params_are_not_rendered(home):
    rows = _composer(
        "c-tool-secret",
        headers=[{"bubbleId": "b1", "type": 2}],
        bubbles={
            "b1": {
                "text": "running",
                "toolFormerData": {
                    "name": "http_get",
                    "status": "completed",
                    "params": {
                        "url": "https://example.test",
                        "apiToken": _SENTINEL_API,
                        "headers": {"authorization": _SENTINEL_API},
                    },
                },
            }
        },
    )
    _make_db(home, rows)

    dumped = json.dumps([s.messages[0].content for s in _sessions(home)])

    assert "https://example.test" in dumped
    assert _SENTINEL_API not in dumped


def test_malformed_sidebar_tabs_and_bubbles_do_not_abort(home):
    payload = {
        "tabs": [
            None,
            "x",
            {"tabId": "t1", "chatTitle": "T" * 5000, "bubbles": [None, 5, {"text": "ok"}]},
            {"tabId": "t2", "bubbles": "nope", "timestamp": float("inf")},
        ]
    }
    rows = {"workbench.panel.aichat.view.aichat.chatdata": json.dumps(payload)}
    rows.update(_composer("c-after", headers=[], extra={"text": 5, "name": None}))
    _make_db(home, rows)

    sessions = _sessions(home)

    sidebar = [s for s in sessions if s.session_id == "sidebar-t1"]
    assert sidebar and len(sidebar[0].title) <= 200
    assert sidebar[0].messages[0].content == "ok"


def test_oversized_composer_title_is_capped(home):
    rows = _composer(
        "c-title",
        name="N" * 50_000,
        headers=[{"bubbleId": "b1", "type": 1}],
        bubbles={"b1": {"text": "hi"}},
    )
    _make_db(home, rows)

    assert len(_sessions(home)[0].title) <= 200
