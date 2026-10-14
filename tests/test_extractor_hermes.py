"""Tests for the Hermes agent extractor (issue #197 / task B2.5)."""

from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path

import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.hermes import HermesExtractor, activity_fingerprint_ns
from lore.utils.tooling import normalize_tool_name

SESSION_A = "sess-aaaa-1111"
SESSION_B = "sess-bbbb-2222"
SHARED_ID = "sess-shared-dup"
PARENT_ID = "parent-sess-01"
CWD = "/work/hermes-app"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _hermes_root(home: Path) -> Path:
    root = home / ".hermes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sessions_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            cwd TEXT,
            git_branch TEXT,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            last_activity_at REAL,
            parent_session_id TEXT,
            end_reason TEXT,
            message_count INTEGER,
            tool_call_count INTEGER
        )
        """
    )


def _messages_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            active INTEGER,
            compacted INTEGER,
            _compressed_summary TEXT,
            display_kind TEXT
        )
        """
    )


def _usage_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE session_model_usage (
            session_id TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER
        )
        """
    )


def _make_db(
    path: Path,
    *,
    sessions: list[tuple] | None = None,
    messages: list[tuple] | None = None,
    usage: list[tuple] | None = None,
    with_messages_table: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        _sessions_schema(conn)
        if with_messages_table:
            _messages_schema(conn)
            _usage_schema(conn)
        for row in sessions or []:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        if with_messages_table:
            for row in messages or []:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, tool_call_id, "
                    "tool_calls, tool_name, timestamp, token_count, finish_reason, "
                    "reasoning, reasoning_content, active, compacted, _compressed_summary, "
                    "display_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
            for row in usage or []:
                conn.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?)", row)
        conn.commit()
    finally:
        conn.close()
    return path


def _extract(home: Path | None = None) -> tuple[HermesExtractor, list]:
    extractor = HermesExtractor()
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


def _rich_session_messages() -> list[tuple]:
    tool_calls = (
        '[{"id":"tc1","call_id":"call-ok","type":"function",'
        '"function":{"name":"read_file","arguments":"{\\"path\\":\\"/x\\"}"}}]'
    )
    return [
        (
            SESSION_A,
            "user",
            "hello",
            None,
            None,
            None,
            1_700_000_000.0,
            None,
            None,
            None,
            None,
            1,
            0,
            None,
            None,
        ),
        (
            SESSION_A,
            "assistant",
            "thinking aloud",
            None,
            tool_calls,
            None,
            1_700_000_001.0,
            50,
            "stop",
            "secret reasoning",
            None,
            1,
            0,
            None,
            None,
        ),
        (
            SESSION_A,
            "tool",
            "file contents",
            "call-ok",
            None,
            "read_file",
            1_700_000_002.0,
            None,
            "stop",
            None,
            None,
            1,
            0,
            None,
            None,
        ),
        (
            SESSION_A,
            "tool",
            "boom",
            "call-bad",
            None,
            "run_cmd",
            1_700_000_003.0,
            None,
            "error",
            None,
            None,
            1,
            0,
            None,
            None,
        ),
        (
            SESSION_A,
            "user",
            "old steer",
            None,
            None,
            None,
            1_700_000_004.0,
            None,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
        ),
        (
            SESSION_A,
            "user",
            "steer note",
            None,
            None,
            None,
            1_700_000_005.0,
            None,
            None,
            None,
            None,
            1,
            0,
            None,
            "steer",
        ),
        (
            SESSION_A,
            "assistant",
            "summary body",
            None,
            None,
            None,
            1_700_000_006.0,
            None,
            None,
            None,
            None,
            1,
            1,
            "compressed chunk",
            None,
        ),
        (
            SESSION_A,
            "session_meta",
            '{"ignored":true}',
            None,
            None,
            None,
            1_700_000_007.0,
            None,
            None,
            None,
            None,
            1,
            0,
            None,
            None,
        ),
    ]


def test_two_profiles_one_session_each(home):
    root = _hermes_root(home)
    alpha = root / "profiles" / "alpha" / "state.db"
    beta = root / "profiles" / "beta" / "state.db"
    _make_db(
        alpha,
        sessions=[
            (SESSION_A, "cli", "Alpha", CWD, "main", "m1", 1.0, 2.0, 2.0, None, None, 1, 0),
        ],
        messages=[
            (
                SESSION_A,
                "user",
                "a1",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "a2",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "a3",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    _make_db(
        beta,
        sessions=[
            (SESSION_B, "telegram", "Beta", CWD, "dev", "m2", 3.0, 4.0, 4.0, None, None, 1, 0),
        ],
        messages=[
            (
                SESSION_B,
                "user",
                "b1",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "assistant",
                "b2",
                None,
                None,
                None,
                4.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "user",
                "b3",
                None,
                None,
                None,
                5.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    _, sessions = _extract()
    assert {s.session_id for s in sessions} == {SESSION_A, SESSION_B}
    by_id = {s.session_id: s for s in sessions}
    assert by_id[SESSION_A].origin == "interactive"
    assert by_id[SESSION_B].origin == "interactive"


def test_tool_call_reasoning_active_compaction(home):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    _make_db(
        db,
        sessions=[
            (SESSION_A, "cli", "Chat", CWD, "main", "gpt", 1.0, 8.0, 8.0, None, None, 8, 2),
        ],
        messages=_rich_session_messages(),
        usage=[(SESSION_A, "gpt", 10, 20, 30)],
    )
    _, sessions = _extract()
    session = sessions[0]
    assistant = next(m for m in session.messages if m.role == Role.ASSISTANT and m.tool_events)
    assert "[Tool: read_file]" in assistant.content
    assert assistant.reasoning == "secret reasoning"
    assert assistant.tool_events[0]["name"] == "read_file"
    assert assistant.tool_events[0]["status"] == "completed"
    inactive = [m for m in session.messages if m.active is False]
    assert inactive and inactive[0].content == "old steer"
    compact = [m for m in session.messages if m.kind == "compaction_summary"]
    assert compact and compact[0].content == "compressed chunk"
    steer = [m for m in session.messages if m.kind == "injected_context"]
    assert steer and steer[0].content == "steer note"
    assert session.messages[-1].role != Role.USER or session.messages[-1].kind != "text"
    assert session.total_tokens == 30


def test_parent_subagent_and_origins(home):
    root = _hermes_root(home)
    db = root / "state.db"
    _make_db(
        db,
        sessions=[
            (PARENT_ID, "cli", "Parent", CWD, None, "m", 1.0, 2.0, 2.0, None, "done", 1, 0),
            (SESSION_A, "cron", "Cron job", CWD, None, "m", 3.0, 4.0, 4.0, PARENT_ID, None, 1, 0),
            (SESSION_B, "oneshot", "Shot", CWD, None, "m", 5.0, 6.0, 6.0, None, None, 1, 0),
            ("sess-null", None, "Unknown", CWD, None, "m", 7.0, 8.0, 8.0, None, None, 1, 0),
        ],
        messages=[
            (
                PARENT_ID,
                "user",
                "p1",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                PARENT_ID,
                "assistant",
                "p2",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                PARENT_ID,
                "user",
                "p3",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "c1",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "c2",
                None,
                None,
                None,
                4.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "c3",
                None,
                None,
                None,
                5.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "user",
                "o1",
                None,
                None,
                None,
                5.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "assistant",
                "o2",
                None,
                None,
                None,
                6.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "user",
                "o3",
                None,
                None,
                None,
                7.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                "sess-null",
                "user",
                "u1",
                None,
                None,
                None,
                7.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                "sess-null",
                "assistant",
                "u2",
                None,
                None,
                None,
                8.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                "sess-null",
                "user",
                "u3",
                None,
                None,
                None,
                9.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    _, sessions = _extract()
    by_id = {s.session_id: s for s in sessions}
    assert by_id[SESSION_A].parent_session_id == PARENT_ID
    assert by_id[SESSION_A].relation == "subagent"
    assert by_id[SESSION_A].origin == "cron"
    assert by_id[SESSION_B].origin == "exec"
    assert by_id["sess-null"].origin == "unknown"


def test_duplicate_session_id_qualified_by_profile(home):
    root = _hermes_root(home)
    for name in ("one", "two"):
        _make_db(
            root / "profiles" / name / "state.db",
            sessions=[
                (SHARED_ID, "cli", name, CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0),
            ],
            messages=[
                (
                    SHARED_ID,
                    "user",
                    "q1",
                    None,
                    None,
                    None,
                    1.0,
                    None,
                    None,
                    None,
                    None,
                    1,
                    0,
                    None,
                    None,
                ),
                (
                    SHARED_ID,
                    "assistant",
                    "a1",
                    None,
                    None,
                    None,
                    2.0,
                    None,
                    None,
                    None,
                    None,
                    1,
                    0,
                    None,
                    None,
                ),
                (
                    SHARED_ID,
                    "user",
                    "q2",
                    None,
                    None,
                    None,
                    3.0,
                    None,
                    None,
                    None,
                    None,
                    1,
                    0,
                    None,
                    None,
                ),
            ],
        )
    _, sessions = _extract()
    ids = {s.session_id for s in sessions}
    # Both copies are qualified, independent of which profile is scanned first.
    assert ids == {"one:" + SHARED_ID, "two:" + SHARED_ID}


def test_hostile_data_does_not_raise(home):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    huge = "x" * 200_000
    bad_tools = "not-json"
    _make_db(
        db,
        sessions=[
            (
                SESSION_A,
                "cli",
                "Hostile",
                CWD,
                None,
                "m",
                float("nan"),
                float("inf"),
                -1.0,
                None,
                None,
                1,
                0,
            ),
        ],
        messages=[
            (
                SESSION_A,
                "user",
                huge,
                None,
                None,
                None,
                float("nan"),
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                bad_tools,
                None,
                bad_tools,
                None,
                float("inf"),
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "tail",
                None,
                None,
                None,
                1_700_000_010.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    conn = sqlite3.connect(db)
    for i in range(5002):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, active) VALUES (?,?,?,1)",
            (
                SESSION_A,
                "user",
                f"filler {i}",
            ),
        )
    conn.commit()
    conn.close()
    extractor, sessions = _extract()
    assert len(sessions) == 1
    assert extractor.record_counts.get("skipped_oversize", 0) >= 1
    assert all(len(m.content) <= 100_000 for m in sessions[0].messages)


def test_corrupt_wrong_schema_and_missing_messages_skipped(home):
    root = _hermes_root(home)
    corrupt = root / "profiles" / "bad" / "state.db"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not-a-sqlite-file")
    wrong = root / "profiles" / "wrong" / "state.db"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(wrong)
    conn.execute("CREATE TABLE sessions (foo TEXT)")
    conn.commit()
    conn.close()
    nomsg = root / "profiles" / "nomsg" / "state.db"
    _make_db(
        nomsg,
        sessions=[(SESSION_A, "cli", "X", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)],
        with_messages_table=False,
    )
    good = root / "profiles" / "good" / "state.db"
    _make_db(
        good,
        sessions=[(SESSION_B, "cli", "OK", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)],
        messages=[
            (
                SESSION_B,
                "user",
                "q",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "assistant",
                "a",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_B,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    extractor, sessions = _extract()
    assert len(sessions) == 1
    assert sessions[0].session_id == SESSION_B
    assert extractor.record_counts.get("skipped_databases", 0) == 3


def test_symlink_and_oversize_db_skipped(home, monkeypatch, tmp_path):
    import lore.extractors.hermes as hermes_mod

    monkeypatch.setattr(hermes_mod, "_MAX_SQLITE_BYTES", 64)
    root = _hermes_root(home)
    big = root / "profiles" / "big" / "state.db"
    _make_db(
        big,
        sessions=[(SESSION_A, "cli", "Big", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)],
        messages=[
            (
                SESSION_A,
                "user",
                "q",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "a",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    link = root / "profiles" / "linked" / "state.db"
    link.parent.mkdir(parents=True)
    link.symlink_to(big)
    extractor, sessions = _extract()
    assert sessions == []
    assert extractor.record_counts.get("skipped_databases", 0) >= 2


def test_original_db_untouched(home):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    _make_db(
        db,
        sessions=[(SESSION_A, "cli", "Keep", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)],
        messages=[
            (
                SESSION_A,
                "user",
                "q",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "a",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    before = db.read_bytes()
    _extract()
    assert db.read_bytes() == before
    assert not db.with_suffix(".db-wal").exists()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_forbidden_files_never_opened(home, monkeypatch):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    _make_db(
        db,
        sessions=[(SESSION_A, "cli", "Safe", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)],
        messages=[
            (
                SESSION_A,
                "user",
                "q",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "a",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    (root / "auth.json").write_text("{}", encoding="utf-8")
    (root / "kanban.db").write_bytes(b"sqlite")
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        text = str(database)
        if "auth.json" in text or "kanban.db" in text:
            raise AssertionError(f"opened forbidden path: {text}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = Path(file) if not isinstance(file, Path) else file
        if path.name in {"auth.json", "kanban.db"}:
            raise AssertionError(f"opened forbidden file: {path.name}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    _, sessions = _extract()
    assert len(sessions) == 1


def test_activity_fingerprint_changes_with_last_activity():
    a = activity_fingerprint_ns(1_700_000_000.0)
    b = activity_fingerprint_ns(1_700_000_001.0)
    assert a is not None and b is not None and a != b
    assert activity_fingerprint_ns(float("nan")) is None
    assert activity_fingerprint_ns(None) is None


def test_source_path_and_fingerprint_on_session(home):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    last = 1_700_000_123.5
    _make_db(
        db,
        sessions=[(SESSION_A, "cli", "Fp", CWD, None, "m", 1.0, 2.0, last, None, None, 1, 0)],
        messages=[
            (
                SESSION_A,
                "user",
                "q",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "assistant",
                "a",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                SESSION_A,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )
    _, sessions = _extract()
    session = sessions[0]
    assert session.source_path == f"{db}#{SESSION_A}"
    assert session.source_mtime_ns == activity_fingerprint_ns(last)


def test_normalize_hermes_aliases():
    assert normalize_tool_name("hermes") == "hermes"
    assert normalize_tool_name("hermes-agent") == "hermes"


def test_hermes_extractor_registered():
    tools = {extractor.tool for extractor in get_all_extractors()}
    assert Tool.HERMES in tools


def test_parser_version_is_one():
    assert HermesExtractor.PARSER_VERSION == 1


def _mini_db(path, session_id, profile_title):
    _make_db(
        path,
        sessions=[
            (session_id, "cli", profile_title, CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0)
        ],
        messages=[
            (
                session_id,
                "user",
                "q1",
                None,
                None,
                None,
                1.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                session_id,
                "assistant",
                "a1",
                None,
                None,
                None,
                2.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                session_id,
                "user",
                "q2",
                None,
                None,
                None,
                3.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
            (
                session_id,
                "assistant",
                "a2",
                None,
                None,
                None,
                4.0,
                None,
                None,
                None,
                None,
                1,
                0,
                None,
                None,
            ),
        ],
    )


def test_session_id_does_not_change_when_another_profile_disappears_and_returns(home):
    root = _hermes_root(home)
    _mini_db(root / "profiles" / "one" / "state.db", SHARED_ID, "one")
    _mini_db(root / "profiles" / "two" / "state.db", SHARED_ID, "two")
    _, both = _extract()
    ids_with_two = {s.session_id for s in both}

    (root / "profiles" / "two" / "state.db").unlink()
    _, only_one = _extract()

    # With a single owner the id is the bare id; with two owners each id is qualified:
    # the qualification depends only on the set of profiles, never on scan order.
    assert ids_with_two == {"one:" + SHARED_ID, "two:" + SHARED_ID}
    assert {s.session_id for s in only_one} == {SHARED_ID}


def test_oversized_tool_calls_and_reasoning_are_bounded_in_sql(home):
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    _mini_db(db, "sess-big", "main")
    import sqlite3

    conn = sqlite3.connect(db)
    huge = json.dumps([{"id": "t1", "function": {"name": "x", "arguments": "a" * 300_000}}])
    conn.execute(
        "UPDATE messages SET tool_calls = ?, reasoning = ? WHERE role = 'assistant'",
        (huge, "r" * 400_000),
    )
    conn.commit()
    conn.close()

    _, sessions = _extract()

    assert len(sessions) == 1
    for m in sessions[0].messages:
        assert len(m.reasoning or "") <= 20_000
        # Oversized tool-call JSON is dropped before parsing: the call is never rendered.
        assert "[Tool: x]" not in (m.content or "")


def test_whole_database_message_budget_stops_loading(home, monkeypatch):
    monkeypatch.setattr("lore.extractors.hermes._MAX_MESSAGES_PER_DB", 5)
    root = _hermes_root(home)
    db = root / "profiles" / "main" / "state.db"
    sessions_rows = []
    messages_rows = []
    for i in range(4):
        sid = f"sess-{i}"
        sessions_rows.append((sid, "cli", f"t{i}", CWD, None, "m", 1.0, 2.0, 2.0, None, None, 1, 0))
        for n, role in enumerate(("user", "assistant", "user", "assistant")):
            messages_rows.append(
                (
                    sid,
                    role,
                    f"{role}{n}",
                    None,
                    None,
                    None,
                    float(n),
                    None,
                    None,
                    None,
                    None,
                    1,
                    0,
                    None,
                    None,
                )
            )
    _make_db(db, sessions=sessions_rows, messages=messages_rows)

    extractor, sessions = _extract()

    assert 1 <= len(sessions) <= 2  # 4 messages each: the budget of 5 allows two at most
    assert extractor.record_counts.get("skipped_oversize", 0) >= 1
