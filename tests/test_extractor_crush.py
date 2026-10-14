"""Tests for the Crush extractor (issue #197 / B2.8c)."""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lore.core.models import Role, Tool
from lore.extractors.crush import CrushExtractor
from lore.extractors.factory import get_all_extractors
from lore.utils.tooling import normalize_tool_name

SESSION_ID = "crush-sess-001"
DUP_ID = "crush-dup-id"
PARENT_ID = "crush-parent-01"
SECRET_SENTINEL = "SENTINEL_NEVER_IN_OUTPUT"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "0")
    return tmp_path


def _share(home: Path) -> Path:
    root = home / ".local" / "share" / "crush"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _crush_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            title TEXT,
            created_at INTEGER,
            updated_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            parts TEXT,
            model TEXT,
            created_at INTEGER,
            is_summary_message INTEGER
        )
        """
    )


def _make_crush_db(db_path: Path, sessions: list, messages: list) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _crush_schema(conn)
        for row in sessions:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", row)
        for row in messages:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, parts, model, created_at, "
                "is_summary_message) VALUES (?,?,?,?,?,?,?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _write_registry(home: Path, projects: list[dict]) -> None:
    share = _share(home)
    (share / "projects.json").write_text(
        json.dumps({"projects": projects}),
        encoding="utf-8",
    )


def _rich_parts() -> str:
    return json.dumps(
        [
            {"type": "text", "data": {"text": "user says hi"}},
            {"type": "reasoning", "data": {"thinking": "think"}},
            {
                "type": "tool_call",
                "data": {
                    "id": "tc1",
                    "name": "fetch",
                    "input": json.dumps({"url": "https://x.test", "token": SECRET_SENTINEL}),
                },
            },
            {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "tc1",
                    "name": "fetch",
                    "content": "ok body",
                    "is_error": False,
                },
            },
            {
                "type": "tool_call",
                "data": {"id": "tc2", "name": "fail", "input": "{}"},
            },
            {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "tc2",
                    "name": "fail",
                    "content": "nope",
                    "is_error": True,
                },
            },
            {
                "type": "shell_command",
                "data": {"command": "echo hi", "output": "hi\n", "exit_code": 0},
            },
        ]
    )


def _project(home: Path, name: str = "crush-app") -> Path:
    return home / "projects" / name


def _setup_project(
    home: Path,
    project: Path,
    *,
    data_dir: str = ".crush",
    session_rows: list | None = None,
    message_rows: list | None = None,
) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    data_path = project / data_dir if not Path(data_dir).is_absolute() else Path(data_dir)
    db = data_path / "crush.db"
    _make_crush_db(
        db,
        session_rows
        or [
            (SESSION_ID, None, "Crush chat", 1_700_000_000, 1_700_000_100),
        ],
        message_rows
        or [
            (
                "m1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "u"}}]),
                None,
                1,
                0,
            ),
            ("m2", SESSION_ID, "assistant", _rich_parts(), "crush-model", 2, 0),
        ],
    )
    _write_registry(
        home,
        [{"path": str(project), "data_dir": data_dir, "last_accessed": "2024-01-01"}],
    )
    return db


def _extract() -> tuple[CrushExtractor, list]:
    return CrushExtractor(), list(CrushExtractor().extract_sessions())


def test_crush_basic_session(home):
    project = _project(home)
    _setup_project(home, project)
    _, sessions = _extract()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool == Tool.CRUSH
    assert s.session_id == SESSION_ID
    assert s.title == "Crush chat"
    assert s.project_path == str(project)
    assert s.origin == "interactive"
    assert SECRET_SENTINEL not in " ".join(m.content for m in s.messages)
    assistant = next(m for m in s.messages if m.role == Role.ASSISTANT)
    assert assistant.model == "crush-model"
    assert assistant.reasoning
    assert any(m.role == Role.TOOL for m in s.messages)


def test_crush_relative_and_absolute_data_dir(home):
    rel_project = _project(home, "crush-rel")
    abs_data = home / "crush-abs-data"
    _setup_project(home, rel_project, data_dir=".crush")
    abs_project = _project(home, "crush-abs-root")
    abs_project.mkdir(parents=True)
    abs_db_dir = abs_data
    _make_crush_db(
        abs_db_dir / "crush.db",
        [(SESSION_ID + "-abs", None, "abs", 1, 2)],
        [
            (
                "a1",
                SESSION_ID + "-abs",
                "user",
                json.dumps([{"type": "text", "data": {"text": "a"}}]),
                None,
                1,
                0,
            ),
        ],
    )
    _write_registry(
        home,
        [
            {"path": str(rel_project), "data_dir": ".crush", "last_accessed": "x"},
            {"path": str(abs_project), "data_dir": str(abs_data), "last_accessed": "x"},
        ],
    )
    _, sessions = _extract()
    assert len(sessions) == 2


def test_crush_parent_subagent(home):
    project = _project(home)
    db = project / ".crush" / "crush.db"
    _make_crush_db(
        db,
        [
            (PARENT_ID, None, "p", 1, 2),
            (SESSION_ID, PARENT_ID, "c", 3, 4),
        ],
        [
            (
                "p1",
                PARENT_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "p"}}]),
                None,
                1,
                0,
            ),
            (
                "c1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "c"}}]),
                None,
                2,
                0,
            ),
        ],
    )
    _write_registry(home, [{"path": str(project), "data_dir": ".crush", "last_accessed": "x"}])
    _, sessions = _extract()
    child = next(s for s in sessions if s.session_id == SESSION_ID)
    assert child.parent_session_id == PARENT_ID
    assert child.relation == "subagent"
    assert child.origin == "subagent"


def test_crush_registry_escape_rejected(home):
    _write_registry(
        home,
        [{"path": "/work/evil", "data_dir": "../escape", "last_accessed": "x"}],
    )
    _, sessions = _extract()
    assert sessions == []


def test_crush_missing_db_skipped(home):
    project = _project(home)
    project.mkdir(parents=True)
    _write_registry(home, [{"path": str(project), "data_dir": ".crush", "last_accessed": "x"}])
    _, sessions = _extract()
    assert sessions == []


def test_crush_duplicate_id_qualified_order_independent(home):
    p1 = _project(home, "crush-a")
    p2 = _project(home, "crush-b")
    db1 = _setup_project(
        home,
        p1,
        session_rows=[(DUP_ID, None, "one", 1, 2)],
        message_rows=[
            (
                "1",
                DUP_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "a"}}]),
                None,
                1,
                0,
            ),
        ],
    )
    db2_path = p2 / ".crush" / "crush.db"
    _make_crush_db(
        db2_path,
        [(DUP_ID, None, "two", 1, 2)],
        [
            (
                "2",
                DUP_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "b"}}]),
                None,
                1,
                0,
            ),
        ],
    )
    _write_registry(
        home,
        [
            {"path": str(p1), "data_dir": ".crush", "last_accessed": "x"},
            {"path": str(p2), "data_dir": ".crush", "last_accessed": "x"},
        ],
    )
    h1 = hashlib.sha256(str(db1.resolve()).encode()).hexdigest()[:8]
    h2 = hashlib.sha256(str(db2_path.resolve()).encode()).hexdigest()[:8]
    _, forward = _extract()
    ids_fwd = {s.session_id for s in forward}
    assert ids_fwd == {f"{h1}:{DUP_ID}", f"{h2}:{DUP_ID}"}
    _, backward = _extract()
    assert {s.session_id for s in backward} == ids_fwd


def test_crush_summary_message(home):
    project = _project(home)
    parts = json.dumps([{"type": "text", "data": {"text": "summary text"}}])
    _setup_project(
        home,
        project,
        message_rows=[
            ("s1", SESSION_ID, "assistant", parts, None, 1, 1),
            (
                "u1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "u"}}]),
                None,
                2,
                0,
            ),
        ],
    )
    _, sessions = _extract()
    kinds = [m.kind for m in sessions[0].messages]
    assert "compaction_summary" in kinds


def test_crush_hidden_text_injected(home):
    project = _project(home)
    hidden = json.dumps([{"type": "text", "data": {"text": "ctx", "hidden": True}}])
    _setup_project(
        home,
        project,
        message_rows=[
            ("h1", SESSION_ID, "assistant", hidden, None, 1, 0),
            (
                "u1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "u"}}]),
                None,
                2,
                0,
            ),
        ],
    )
    _, sessions = _extract()
    assert any(m.kind == "injected_context" for m in sessions[0].messages)


def test_crush_original_db_untouched(home):
    db = _setup_project(home, _project(home))
    before = db.read_bytes()
    _extract()
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()


def test_crush_symlink_db_skipped(home, tmp_path):
    project = _project(home)
    project.mkdir(parents=True)
    real = tmp_path / "real.db"
    _make_crush_db(
        real,
        [(SESSION_ID, None, "t", 1, 2)],
        [
            (
                "1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "x"}}]),
                None,
                1,
                0,
            ),
        ],
    )
    data_dir = project / ".crush"
    data_dir.mkdir(parents=True)
    (data_dir / "crush.db").symlink_to(real)
    _write_registry(home, [{"path": str(project), "data_dir": ".crush", "last_accessed": "x"}])
    _, sessions = _extract()
    assert sessions == []


def test_normalize_crush_aliases():
    assert normalize_tool_name("crush") == "crush"
    assert normalize_tool_name("charm-crush") == "crush"


def test_crush_registered():
    tools = {e.tool for e in get_all_extractors()}
    assert Tool.CRUSH in tools
    assert CrushExtractor.PARSER_VERSION == 1


def test_crush_forbidden_neighbor_files(home, monkeypatch):
    db = _setup_project(home, _project(home))
    (db.parent / "crush.json").write_text("{}", encoding="utf-8")
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if "crush.json" in str(file):
            raise OSError("forbidden")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_crush_malformed_tool_input_never_leaks_secret_prefix(home):
    parts = json.dumps(
        [
            {"type": "text", "data": {"text": "calling"}},
            {
                "type": "tool_call",
                "data": {"id": "c1", "name": "http", "input": '{"api_key":"SENTINEL_KEY'},
            },
        ]
    )
    _setup_project(
        home,
        _project(home, "leak-app"),
        message_rows=[
            (
                "m1",
                SESSION_ID,
                "user",
                json.dumps([{"type": "text", "data": {"text": "u"}}]),
                None,
                1,
                0,
            ),
            ("m2", SESSION_ID, "assistant", parts, "m", 2, 0),
        ],
    )

    _, sessions = _extract()

    text = " ".join(m.content for s in sessions for m in s.messages)
    assert "calling" in text
    assert "SENTINEL_KEY" not in text


def test_crush_oversized_registry_ignored(home, monkeypatch):
    import lore.extractors.crush as crush_mod

    _setup_project(home, _project(home, "big-registry"))
    monkeypatch.setattr(crush_mod, "_MAX_REGISTRY_BYTES", 8)

    _, sessions = _extract()

    assert sessions == []
