"""Tests for the Zed agent extractor (issue #197 / B2.8e)."""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
from pathlib import Path

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.zed import PARSER_VERSION, ZedExtractor
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "zed-thread-001"
PARENT_ID = "zed-parent-01"
SECRET_SENTINEL = "SENTINEL_NEVER_IN_OUTPUT"
RAW_SENTINEL = "RAW_INPUT_SENTINEL"
SIG_SENTINEL = "SIGNATURE_SENTINEL"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


def _db_path(home: Path) -> Path:
    return home / ".local" / "share" / "zed" / "threads" / "threads.db"


def _workdir(home: Path) -> str:
    path = home / "zed-demo"
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def _make_threads_db(
    path: Path,
    rows: list[tuple],
    *,
    schema: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if schema is None:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    data BLOB NOT NULL,
                    parent_id TEXT,
                    folder_paths TEXT,
                    created_at TEXT
                )
                """
            )
        else:
            conn.execute(schema)
        for row in rows:
            conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", row)
        conn.commit()
    finally:
        conn.close()
    return path


def _thread_payload(**overrides) -> dict:
    messages = overrides.pop(
        "messages",
        [
            {"User": {"id": "u1", "content": [{"Text": "hello user"}]}},
            {
                "Agent": {
                    "content": [
                        {"Text": "assistant says hi"},
                        {"Thinking": {"text": "inner plan", "signature": SIG_SENTINEL}},
                        {
                            "ToolUse": {
                                "id": "call-1",
                                "name": "read_file",
                                "raw_input": RAW_SENTINEL,
                                "input": {"path": "/tmp/x", "api_key": SECRET_SENTINEL},
                                "is_input_complete": True,
                            }
                        },
                    ],
                    "tool_results": {
                        "call-1": {
                            "tool_use_id": "call-1",
                            "tool_name": "read_file",
                            "is_error": False,
                            "content": {"Text": "file body"},
                        }
                    },
                }
            },
            "Resume",
            {"Compaction": {"Summary": "compacted context"}},
        ],
    )
    data = {
        "title": "JSON title fallback",
        "messages": messages,
        "updated_at": "2024-03-15T12:00:00Z",
        "cumulative_token_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
        },
        "model": {"provider": "anthropic", "model": "claude-test"},
    }
    data.update(overrides)
    return data


def _json_row(
    thread_id: str,
    summary: str,
    workdir: str,
    payload: dict | None = None,
    *,
    parent_id: str | None = None,
    bad_paths: str | None = None,
) -> tuple:
    blob = json.dumps(payload or _thread_payload()).encode("utf-8")
    paths = bad_paths if bad_paths is not None else workdir
    return (
        thread_id,
        summary,
        "2024-03-15T12:00:00Z",
        "json",
        blob,
        parent_id,
        paths,
        "2024-03-15T10:00:00Z",
    )


def _extract(extractor: ZedExtractor | None = None):
    extractor = extractor or ZedExtractor()
    return extractor, list(extractor.extract_sessions())


def test_zed_basic_json_thread(home):
    workdir = _workdir(home)
    _make_threads_db(_db_path(home), [_json_row(SESSION_ID, "Native summary", workdir)])
    _, sessions = _extract()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.ZED
    assert session.title == "Native summary"
    assert session.project_path == workdir
    assert session.origin == "interactive"
    dumped = json.dumps([m.__dict__ for m in session.messages], default=str)
    assert SECRET_SENTINEL not in dumped
    assert RAW_SENTINEL not in dumped
    assert SIG_SENTINEL not in dumped
    assistant = next(m for m in session.messages if m.role == Role.ASSISTANT)
    assert assistant.reasoning and "inner plan" in assistant.reasoning
    assert assistant.model == "anthropic/claude-test"
    assert assistant.tokens == {
        "input": 100,
        "output": 50,
        "cache_read": 10,
        "cache_write": 5,
    }
    assert any(m.role == Role.TOOL for m in session.messages)
    assert any(m.kind == "compaction_summary" for m in session.messages)


def test_zed_parent_subagent(home):
    workdir = _workdir(home)
    _make_threads_db(
        _db_path(home),
        [
            _json_row(PARENT_ID, "parent", workdir),
            _json_row(SESSION_ID, "child", workdir, parent_id=PARENT_ID),
        ],
    )
    _, sessions = _extract()
    child = next(s for s in sessions if s.session_id == SESSION_ID)
    assert child.parent_session_id == PARENT_ID
    assert child.relation == "subagent"
    assert child.origin == "subagent"


def test_zed_folder_paths_rejects_dotdot(home):
    workdir = _workdir(home)
    bad = "/bad/../escape"
    _make_threads_db(
        _db_path(home),
        [_json_row(SESSION_ID, "t", workdir, bad_paths=bad)],
    )
    _, sessions = _extract()
    assert sessions[0].project_path is None


def test_zed_missing_zstd_module_counts_skip(home, monkeypatch):
    zstd = pytest.importorskip("zstandard")
    workdir = _workdir(home)
    payload = _thread_payload()
    compressed = zstd.ZstdCompressor().compress(json.dumps(payload).encode("utf-8"))
    row = (
        "zstd-thread-02",
        "zstd row",
        "2024-03-15T12:00:00Z",
        "zstd",
        compressed,
        None,
        workdir,
        "2024-03-15T10:00:00Z",
    )
    _make_threads_db(_db_path(home), [_json_row(SESSION_ID, "json", workdir), row])

    real_import = builtins.__import__

    def block_zstandard(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_zstandard)
    extractor, sessions = _extract()
    assert {s.session_id for s in sessions} == {SESSION_ID}
    assert extractor.discovery.get("skipped_missing_zstd", 0) >= 1


def test_zed_zstd_row_decoded():
    zstd = pytest.importorskip("zstandard")
    workdir = "/work/zed-zstd"
    payload = _thread_payload(messages=[{"User": {"id": "u", "content": [{"Text": "z"}]}}])
    compressed = zstd.ZstdCompressor().compress(json.dumps(payload).encode("utf-8"))
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.environ["HOME"] = str(home)
        row = (
            "zstd-only",
            "z",
            "2024-03-15T12:00:00Z",
            "zstd",
            compressed,
            None,
            workdir,
            "2024-03-15T10:00:00Z",
        )
        _make_threads_db(_db_path(home), [row])
        _, sessions = _extract()
        assert len(sessions) == 1


def test_zed_corrupt_blob_and_db_skipped(home):
    workdir = _workdir(home)
    bad_row = (
        SESSION_ID,
        "bad",
        "2024-03-15T12:00:00Z",
        "json",
        b"not-json",
        None,
        workdir,
        "2024-03-15T10:00:00Z",
    )
    _make_threads_db(_db_path(home), [bad_row])
    _, sessions = _extract()
    assert sessions == []

    corrupt = _db_path(home)
    corrupt.write_bytes(b"not-sqlite")
    _, sessions2 = _extract()
    assert sessions2 == []


def test_zed_oversized_compressed_skipped(home, monkeypatch):
    import lore.extractors.zed as zed_mod

    monkeypatch.setattr(zed_mod, "_MAX_COMPRESSED_BLOB", 64)
    workdir = _workdir(home)
    big = b"x" * 128
    row = (
        SESSION_ID,
        "big",
        "2024-03-15T12:00:00Z",
        "zstd",
        big,
        None,
        workdir,
        "2024-03-15T10:00:00Z",
    )
    _make_threads_db(_db_path(home), [row])
    _, sessions = _extract()
    assert sessions == []


def test_zed_original_db_untouched(home):
    workdir = _workdir(home)
    db = _make_threads_db(_db_path(home), [_json_row(SESSION_ID, "t", workdir)])
    before = db.read_bytes()
    _extract()
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_zed_symlink_db_skipped(home, tmp_path):
    workdir = _workdir(home)
    real = tmp_path / "real.db"
    _make_threads_db(real, [_json_row(SESSION_ID, "t", workdir)])
    link = _db_path(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    _, sessions = _extract()
    assert sessions == []


def test_zed_hostile_json_does_not_abort(home):
    workdir = _workdir(home)
    nested = {"a": None}
    cur = nested
    for _ in range(200):
        cur["a"] = {"a": None}
        cur = cur["a"]
    payload = _thread_payload(messages=[nested, {"User": {"id": "u", "content": [{"Text": "ok"}]}}])
    _make_threads_db(_db_path(home), [_json_row(SESSION_ID, "t", workdir, payload)])
    _, sessions = _extract()
    assert len(sessions) == 1


def test_normalize_zed_aliases():
    assert normalize_tool_name("zed") == "zed"
    assert normalize_tool_name("zed-agent") == "zed"


def test_zed_registered():
    assert Tool.ZED in {e.tool for e in get_all_extractors()}


def test_zed_parser_version():
    assert PARSER_VERSION == 1


def test_zed_schema_without_updated_at_column_still_read(home):
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, data_type TEXT, data BLOB)")
    blob = json.dumps(_thread_payload()).encode("utf-8")
    conn.execute("INSERT INTO threads VALUES ('t-min', 'json', ?)", (blob,))
    conn.commit()
    conn.close()

    _, sessions = _extract()

    assert [s.session_id for s in sessions] == ["t-min"]
