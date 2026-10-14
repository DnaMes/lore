"""Tests for the Goose extractor (issue #197 / B2.8c)."""

from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.goose import PARSER_VERSION, GooseExtractor
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "goose-sess-001"
PARENT_ID = "goose-parent-01"
SECRET_SENTINEL = "SENTINEL_NEVER_IN_OUTPUT"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


@pytest.fixture
def workdir(home) -> str:
    return _workdir(home)


def _db_path(home: Path) -> Path:
    return home / ".local" / "share" / "goose" / "sessions" / "sessions.db"


def _workdir(home: Path) -> str:
    path = home / "goose-demo"
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def _sessions_schema(conn: sqlite3.Connection, *, minimal: bool = False) -> None:
    if minimal:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, working_dir TEXT)")
    else:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT,
                working_dir TEXT,
                created_at TEXT,
                updated_at TEXT,
                session_type TEXT,
                schedule_id TEXT,
                parent_session_id TEXT,
                model_config_json TEXT
            )
            """
        )


def _messages_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            session_id TEXT,
            role TEXT,
            content_json TEXT,
            created_timestamp INTEGER,
            timestamp TEXT,
            metadata_json TEXT
        )
        """
    )


def _make_db(
    path: Path,
    *,
    sessions: list[tuple] | None = None,
    messages: list[tuple] | None = None,
    minimal_schema: bool = False,
    with_messages: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        _sessions_schema(conn, minimal=minimal_schema)
        if with_messages:
            _messages_schema(conn)
        for row in sessions or []:
            if minimal_schema:
                conn.execute("INSERT INTO sessions VALUES (?,?,?)", row)
            else:
                conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", row)
        if with_messages:
            for row in messages or []:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content_json, created_timestamp, "
                    "metadata_json) VALUES (?,?,?,?,?)",
                    row,
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _extract(home: Path | None = None) -> tuple[GooseExtractor, list]:
    extractor = GooseExtractor()
    return extractor, list(extractor.extract_sessions())


def _rich_messages() -> list[tuple]:
    user_blocks = json.dumps([{"type": "text", "text": "hello user"}])
    hidden_meta = json.dumps({"userVisible": False})
    hidden_blocks = json.dumps([{"type": "text", "text": "hidden ctx"}])
    thinking = json.dumps(
        [
            {"type": "thinking", "thinking": "plan steps", "signature": "sig"},
            {"type": "text", "text": "assistant says hi"},
        ]
    )
    tool_ok_req = json.dumps(
        [
            {
                "type": "toolRequest",
                "id": "call-ok",
                "toolCall": {
                    "status": "success",
                    "value": {
                        "name": "read_file",
                        "arguments": {"path": "/tmp/x", "api_key": SECRET_SENTINEL},
                    },
                },
            }
        ]
    )
    tool_ok_resp = json.dumps(
        [
            {
                "type": "toolResponse",
                "id": "call-ok",
                "toolResult": {
                    "status": "success",
                    "value": {
                        "content": [{"type": "text", "text": "file body"}],
                        "isError": False,
                    },
                },
            }
        ]
    )
    tool_fail_resp = json.dumps(
        [
            {
                "type": "toolRequest",
                "id": "call-bad",
                "toolCall": {
                    "status": "success",
                    "value": {"name": "run_cmd", "arguments": {}},
                },
            },
            {
                "type": "toolResponse",
                "id": "call-bad",
                "toolResult": {"status": "error", "error": "failed"},
            },
        ]
    )
    return [
        (SESSION_ID, "user", user_blocks, 1_700_000_000, None),
        (SESSION_ID, "user", hidden_blocks, 1_700_000_001, hidden_meta),
        (SESSION_ID, "assistant", thinking, 1_700_000_002, None),
        (SESSION_ID, "assistant", tool_ok_req, 1_700_000_003, None),
        (SESSION_ID, "assistant", tool_ok_resp, 1_700_000_004, None),
        (SESSION_ID, "assistant", tool_fail_resp, 1_700_000_005, None),
    ]


def test_goose_basic_session(home, workdir):
    _make_db(
        _db_path(home),
        sessions=[
            (
                SESSION_ID,
                "My Goose chat",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 11:00:00",
                "user",
                None,
                None,
                json.dumps({"model_name": "test-model"}),
            )
        ],
        messages=_rich_messages(),
    )
    _, sessions = _extract()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == Tool.GOOSE
    assert session.session_id == SESSION_ID
    assert session.title == "My Goose chat"
    assert session.project_path == workdir
    assert session.origin == "interactive"
    roles = [m.role for m in session.messages]
    assert Role.USER in roles
    assert Role.ASSISTANT in roles
    assert Role.TOOL in roles
    assistant = next(m for m in session.messages if m.role == Role.ASSISTANT)
    assert assistant.reasoning and "plan steps" in assistant.reasoning
    assert assistant.model == "test-model"
    assert SECRET_SENTINEL not in " ".join(m.content for m in session.messages)
    injected = [m for m in session.messages if m.kind == "injected_context"]
    assert injected
    assert session.user_prompt_count >= 1


def test_goose_parent_subagent_and_cron(home, workdir):
    _make_db(
        _db_path(home),
        sessions=[
            (
                PARENT_ID,
                "parent",
                workdir,
                "bad-ts",
                None,
                "scheduled",
                "sched-1",
                None,
                None,
            ),
            (
                SESSION_ID,
                "child",
                workdir,
                "2024-01-02 10:00:00",
                "garbage-date",
                "user",
                None,
                PARENT_ID,
                None,
            ),
        ],
        messages=[
            (PARENT_ID, "user", json.dumps([{"type": "text", "text": "p"}]), 1, None),
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "c"}]), 2, None),
        ],
    )
    _, sessions = _extract()
    by_id = {s.session_id: s for s in sessions}
    assert by_id[PARENT_ID].origin == "cron"
    child = by_id[SESSION_ID]
    assert child.parent_session_id == PARENT_ID
    assert child.relation == "subagent"
    assert child.origin == "subagent"


def test_goose_rejects_bad_working_dir(home):
    _make_db(
        _db_path(home),
        sessions=[
            (
                SESSION_ID,
                "t",
                "/bad/../escape",
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=[
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "x"}]), 1, None),
        ],
    )
    _, sessions = _extract()
    assert sessions[0].project_path is None


def test_goose_hostile_content_does_not_raise(home, workdir):
    _make_db(
        _db_path(home),
        sessions=[
            (
                SESSION_ID,
                "t",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=[
            (SESSION_ID, "user", "not-json", 1, None),
            (SESSION_ID, "user", json.dumps({"oops": "dict"}), 2, None),
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "z" * 150_000}]), 3, None),
        ],
    )
    _, sessions = _extract()
    assert len(sessions) == 1
    assert all(len(m.content) <= 100_000 for m in sessions[0].messages)


def test_goose_wrong_schema_and_corrupt_skipped(home, workdir):
    db = _db_path(home)
    _make_db(
        db,
        sessions=[(SESSION_ID, "t", workdir)],
        with_messages=False,
        minimal_schema=True,
    )
    _, sessions = _extract()
    assert sessions == []

    corrupt = _db_path(home)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not-sqlite")
    _, sessions2 = _extract()
    assert sessions2 == []


def test_goose_original_db_untouched(home, workdir):
    db = _db_path(home)
    _make_db(
        db,
        sessions=[
            (
                SESSION_ID,
                "t",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=[
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "x"}]), 1, None),
        ],
    )
    before = db.read_bytes()
    _extract()
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_goose_symlink_db_skipped(home, workdir, tmp_path):
    real = tmp_path / "real.db"
    _make_db(
        real,
        sessions=[
            (
                SESSION_ID,
                "t",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=[
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "x"}]), 1, None),
        ],
    )
    link = _db_path(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    _, sessions = _extract()
    assert sessions == []


def test_goose_minimal_schema_columns(home, workdir):
    _make_db(
        _db_path(home),
        sessions=[(SESSION_ID, "minimal title", workdir)],
        messages=[
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "hi"}]), 1, None),
        ],
        minimal_schema=True,
    )
    _, sessions = _extract()
    assert len(sessions) == 1
    assert sessions[0].title == "minimal title"


def test_goose_tool_events_failed(home, workdir):
    _make_db(
        _db_path(home),
        sessions=[
            (
                SESSION_ID,
                "t",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=_rich_messages(),
    )
    _, sessions = _extract()
    assistants = [m for m in sessions[0].messages if m.role == Role.ASSISTANT]
    failed = [ev for m in assistants for ev in (m.tool_events or []) if ev.get("name") == "run_cmd"]
    assert failed and failed[0]["status"] == "failed"


def test_normalize_goose_aliases():
    assert normalize_tool_name("goose") == "goose"
    assert normalize_tool_name("goose-cli") == "goose"
    assert normalize_tool_name("block-goose") == "goose"


def test_goose_registered():
    tools = {e.tool for e in get_all_extractors()}
    assert Tool.GOOSE in tools
    assert GooseExtractor.PARSER_VERSION == PARSER_VERSION == 1


def test_goose_forbidden_neighbor_files(home, workdir, monkeypatch):
    db = _db_path(home)
    _make_db(
        db,
        sessions=[
            (
                SESSION_ID,
                "t",
                workdir,
                "2024-01-02 10:00:00",
                "2024-01-02 10:00:00",
                "user",
                None,
                None,
                None,
            )
        ],
        messages=[
            (SESSION_ID, "user", json.dumps([{"type": "text", "text": "x"}]), 1, None),
        ],
    )
    secrets = db.parent / "config.yaml"
    secrets.write_text("secret", encoding="utf-8")
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file)
        if "config.yaml" in path:
            raise OSError(f"forbidden: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_goose_messages_without_timestamp_column_keep_rowid_order(home, workdir):
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, working_dir TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content_json TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'No ts', ?)", (workdir,))
    for role, text in (("user", "first"), ("assistant", "second")):
        conn.execute(
            "INSERT INTO messages VALUES ('s1', ?, ?)",
            (role, json.dumps([{"type": "text", "text": text}])),
        )
    conn.commit()
    conn.close()

    _, sessions = _extract(home)

    assert [m.content for m in sessions[0].messages] == ["first", "second"]


def test_goose_deeply_nested_content_does_not_abort(home, workdir):
    nested = "[" * 500_000 + "]" * 500_000
    _make_db(
        _db_path(home),
        sessions=[
            ("s-deep", "Deep", workdir, None, None, "user", None, None, None),
            ("s-ok", "Fine", workdir, None, None, "user", None, None, None),
        ],
        messages=[
            ("s-deep", "user", nested, 1, None),
            ("s-ok", "user", json.dumps([{"type": "text", "text": "still here"}]), 1, None),
        ],
    )

    _, sessions = _extract(home)

    assert any(s.session_id == "s-ok" for s in sessions)
