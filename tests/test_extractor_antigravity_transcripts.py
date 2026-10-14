"""Transcript and conversation_summaries.db coverage for Antigravity (B2.3)."""

from __future__ import annotations

import builtins
import json
import sqlite3
import time
from pathlib import Path

import pytest

from lore.core.models import Role, TitleSource, Tool
from lore.extractors.antigravity import AntigravityExtractor


def _brain(home: Path, variant: str = "antigravity") -> Path:
    path = home / ".gemini" / variant / "brain"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_dir(home: Path, session_id: str, variant: str = "antigravity") -> Path:
    d = _brain(home, variant) / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_transcript(
    session_dir: Path,
    records: list[dict],
    *,
    use_full: bool = True,
    also_short: bool = False,
) -> Path:
    logs = session_dir / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in records) + "\n"
    if use_full:
        path = logs / "transcript_full.jsonl"
        path.write_text(text, encoding="utf-8")
        if also_short:
            (logs / "transcript.jsonl").write_text('{"type":"USER_INPUT"}\n', encoding="utf-8")
        return path
    path = logs / "transcript.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def _summaries_db(home: Path, rows: list[tuple]) -> Path:
    base = home / ".gemini" / "antigravity"
    base.mkdir(parents=True, exist_ok=True)
    db = base / "conversation_summaries.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER, "
        "last_modified_time TEXT, workspace_uris TEXT, agent_name TEXT, "
        "parent_conversation_id TEXT, nesting_depth INTEGER, project_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _extract(home: Path) -> tuple[AntigravityExtractor, list]:
    extractor = AntigravityExtractor()
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "1")
    return tmp_path


def test_parser_version_bumped():
    assert AntigravityExtractor.PARSER_VERSION == 2


def test_transcript_roles_reasoning_and_tool_event(home):
    sid = "conv-roles-01"
    session_dir = _session_dir(home, sid)
    _write_transcript(
        session_dir,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "run the tool",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2025-06-15T10:00:01Z",
                "content": "I'll call it.",
                "thinking": "need to invoke grep",
                "tool_calls": [{"name": "grep", "args": {"pattern": "foo"}}],
                "status": "completed",
            },
            {
                "step_index": 2,
                "source": "SYSTEM",
                "type": "GENERIC",
                "created_at": "2025-06-15T10:00:02Z",
                "content": "tool output line",
            },
        ],
    )

    _, sessions = _extract(home)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.messages[0].role is Role.USER
    assistant = s.messages[1]
    assert assistant.role is Role.ASSISTANT
    assert assistant.reasoning == "need to invoke grep"
    assert "need to invoke grep" not in assistant.content
    assert "[Tool: grep]" in assistant.content
    assert assistant.tool_events[0]["name"] == "grep"
    assert s.messages[2].role is Role.TOOL


def test_transcript_full_preferred_over_short(home):
    sid = "conv-prefer-full"
    session_dir = _session_dir(home, sid)
    logs = session_dir / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "from short",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "transcript_full.jsonl").write_text(
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "from full",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, sessions = _extract(home)
    assert sessions[0].messages[0].content == "from full"


def test_antigravity_cli_and_ide_roots(home):
    for variant, sid, text in (
        ("antigravity-cli", "cli-1", "cli prompt"),
        ("antigravity-ide", "ide-1", "ide prompt"),
    ):
        session_dir = _session_dir(home, sid, variant=variant)
        _write_transcript(
            session_dir,
            [
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2025-06-15T10:00:00Z",
                    "content": text,
                }
            ],
        )

    _, sessions = _extract(home)
    assert {s.session_id for s in sessions} == {"cli-1", "ide-1"}


def test_injected_context_not_counted_as_user_prompt(home):
    sid = "conv-inject"
    session_dir = _session_dir(home, sid)
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "real user",
            },
            {
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "created_at": "2025-06-15T10:00:01Z",
                "content": "system ctx",
            },
            {
                "source": "SYSTEM",
                "type": "CHECKPOINT",
                "created_at": "2025-06-15T10:00:02Z",
                "content": "checkpoint",
            },
        ],
    )

    _, sessions = _extract(home)
    s = sessions[0]
    assert s.user_prompt_count == 1
    kinds = [m.kind for m in s.messages if m.role is Role.INFO]
    assert kinds == ["injected_context", "injected_context"]


def test_error_message_is_info(home):
    sid = "conv-err"
    session_dir = _session_dir(home, sid)
    _write_transcript(
        session_dir,
        [
            {
                "source": "SYSTEM",
                "type": "ERROR_MESSAGE",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "something broke",
            },
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:01Z",
                "content": "user follow-up",
            },
        ],
    )

    _, sessions = _extract(home)
    assert sessions[0].messages[0].role is Role.INFO
    assert sessions[0].messages[0].kind == "text"


def test_unknown_type_counted_once(home):
    sid = "conv-unknown"
    session_dir = _session_dir(home, sid)
    _write_transcript(
        session_dir,
        [
            {
                "source": "MODEL",
                "type": "FUTURE_TYPE",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "x",
            },
            {
                "source": "MODEL",
                "type": "FUTURE_TYPE",
                "created_at": "2025-06-15T10:00:01Z",
                "content": "y",
            },
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:02Z",
                "content": "user",
            },
        ],
    )

    extractor, sessions = _extract(home)
    assert len(sessions) == 1
    assert extractor.discovery.get("records_unknown") == 1


def test_fallback_task_md_without_transcript(home):
    session_dir = _session_dir(home, "legacy-task")
    (session_dir / "task.md").write_text("# Legacy\n\nGoal from task.", encoding="utf-8")

    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert sessions[0].messages[0].role is Role.USER
    assert "Goal from task" in sessions[0].messages[0].content


def test_native_title_and_workspace_from_summaries_db(home):
    project = home / "workspace" / "demo"
    project.mkdir(parents=True)
    uri = json.dumps([project.as_uri()])
    _summaries_db(
        home,
        [
            (
                "conv-meta-1",
                "Native session title",
                None,
                1,
                None,
                uri,
                None,
                None,
                0,
                None,
            )
        ],
    )
    session_dir = _session_dir(home, "conv-meta-1")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "hello",
            }
        ],
    )

    _, sessions = _extract(home)
    s = sessions[0]
    assert s.title == "Native session title"
    assert s.title_source is TitleSource.NATIVE
    assert s.project_path == str(project)


def test_parent_conversation_subagent_fields(home):
    _summaries_db(
        home,
        [
            (
                "child-1",
                "Child",
                None,
                1,
                None,
                None,
                None,
                "parent-9",
                1,
                None,
            )
        ],
    )
    session_dir = _session_dir(home, "child-1")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "sub work",
            }
        ],
    )

    _, sessions = _extract(home)
    s = sessions[0]
    assert s.parent_session_id == "parent-9"
    assert s.relation == "subagent"
    assert s.origin == "subagent"


def test_summaries_db_not_modified(home):
    db = _summaries_db(
        home,
        [
            (
                "conv-db-safe",
                "T",
                None,
                1,
                None,
                None,
                None,
                None,
                0,
                None,
            )
        ],
    )
    before = db.read_bytes()
    session_dir = _session_dir(home, "conv-db-safe")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "x",
            }
        ],
    )
    _extract(home)
    assert db.read_bytes() == before
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_corrupt_summaries_db_falls_back(home):
    base = home / ".gemini" / "antigravity"
    base.mkdir(parents=True)
    (base / "conversation_summaries.db").write_bytes(b"not sqlite" * 10)
    session_dir = _session_dir(home, "conv-corrupt-db")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "still works",
            }
        ],
    )
    _, sessions = _extract(home)
    assert len(sessions) == 1


def test_symlinked_summaries_db_falls_back(home, tmp_path):
    real = tmp_path / "real.db"
    real.write_bytes(b"x" * 32)
    base = home / ".gemini" / "antigravity"
    base.mkdir(parents=True)
    db = base / "conversation_summaries.db"
    db.symlink_to(real)
    session_dir = _session_dir(home, "conv-symlink-db")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "ok",
            }
        ],
    )
    _, sessions = _extract(home)
    assert len(sessions) == 1


def test_oversized_sqlite_sidecar_disables_summaries(home, monkeypatch):
    _summaries_db(
        home,
        [
            (
                "conv-big-wal",
                "Should Not Apply",
                None,
                1,
                None,
                None,
                None,
                None,
                0,
                None,
            )
        ],
    )
    db = home / ".gemini" / "antigravity" / "conversation_summaries.db"
    Path(f"{db}-wal").write_bytes(b"x" * 4096)
    monkeypatch.setattr("lore.extractors.antigravity._MAX_SQLITE_BYTES", db.stat().st_size + 50)
    session_dir = _session_dir(home, "conv-big-wal")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "hello",
            }
        ],
    )
    _, sessions = _extract(home)
    assert sessions[0].title != "Should Not Apply"


def test_pb_files_never_opened(home, monkeypatch):
    conv = home / ".gemini" / "antigravity" / "conversations"
    conv.mkdir(parents=True)
    pb = conv / "secret.pb"
    pb.write_bytes(b"\x00" * 8)
    pb.chmod(0o000)

    real_open = builtins.open
    opened_pb: list[str] = []

    def tracking_open(file, *args, **kwargs):
        path = str(file)
        if path.endswith(".pb"):
            opened_pb.append(path)
            raise OSError("must not open pb")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    session_dir = _session_dir(home, "conv-no-pb")
    _write_transcript(
        session_dir,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "safe",
            }
        ],
    )
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert opened_pb == []


def test_hostile_input_does_not_abort(home, monkeypatch):
    session_dir = _session_dir(home, "conv-hostile")
    logs = session_dir / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    lines = [
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": 1e999,
                "content": "bad ts",
            }
        ),
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": {"not": "a string"},
            }
        ),
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:01Z",
                "content": "good",
            }
        ),
    ]
    (logs / "transcript_full.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr("lore.extractors.antigravity._MAX_LINE_CHARS", 100)
    huge = "x" * 200
    (logs / "transcript_full.jsonl").write_text(
        (logs / "transcript_full.jsonl").read_text() + huge + "\n",
        encoding="utf-8",
    )

    symlink_target = _session_dir(home, "real-target")
    _write_transcript(
        symlink_target,
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2025-06-15T10:00:00Z",
                "content": "hidden",
            }
        ],
    )
    brain = home / ".gemini" / "antigravity" / "brain"
    link = brain / "symlink-session"
    if link.exists():
        link.unlink()
    link.symlink_to(symlink_target, target_is_directory=True)

    _, sessions = _extract(home)
    ids = {s.session_id for s in sessions}
    assert "conv-hostile" in ids
    assert "symlink-session" not in ids


def test_linear_time_twenty_thousand_records(home):
    sid = "conv-linear"
    session_dir = _session_dir(home, sid)
    records = []
    for i in range(10_000):
        records.append(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": f"2025-06-15T10:{i % 60:02d}:00Z",
                "content": f"prompt {i}",
            }
        )
        records.append(
            {
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": f"2025-06-15T10:{i % 60:02d}:01Z",
                "content": f"reply {i}",
            }
        )
    _write_transcript(session_dir, records)

    start = time.monotonic()
    _, sessions = _extract(home)
    elapsed = time.monotonic() - start
    assert len(sessions) == 1
    assert len(sessions[0].messages) == 20_000
    assert elapsed < 20.0


def test_tool_property_unchanged(home):
    assert AntigravityExtractor().tool is Tool.ANTIGRAVITY


# ---------------------------------------------------------------------------
# Review findings (B2.3): descriptor-based copy, brain symlink, duplicate roots, no .pb listing
# ---------------------------------------------------------------------------


def test_copy_helper_rejects_symlinked_database_and_sidecars(tmp_path):
    from lore.utils.sqlite_copy import copy_sqlite_with_sidecars

    real = tmp_path / "real.db"
    real.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
    link = tmp_path / "link.db"
    link.symlink_to(real)
    dest = tmp_path / "dest"
    dest.mkdir()

    assert copy_sqlite_with_sidecars(link, dest, 10_000) is None

    db = tmp_path / "plain.db"
    db.write_bytes(b"x" * 32)
    (tmp_path / "plain.db-wal").symlink_to(real)
    assert copy_sqlite_with_sidecars(db, dest, 10_000) is None


def test_copy_helper_enforces_the_combined_cap_while_copying(tmp_path):
    from lore.utils.sqlite_copy import copy_sqlite_with_sidecars

    db = tmp_path / "a.db"
    db.write_bytes(b"x" * 600)
    (tmp_path / "a.db-wal").write_bytes(b"y" * 600)
    dest = tmp_path / "dest"
    dest.mkdir()

    assert copy_sqlite_with_sidecars(db, dest, 1000) is None
    assert copy_sqlite_with_sidecars(db, dest, 2000) == dest / "a.db"
    assert (dest / "a.db-wal").read_bytes() == b"y" * 600


def test_copy_helper_survives_a_swap_between_check_and_copy(tmp_path, monkeypatch):
    """The file is opened with O_NOFOLLOW: a symlink swapped in later is never read."""
    import os

    from lore.utils import sqlite_copy

    db = tmp_path / "swap.db"
    db.write_bytes(b"x" * 16)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    real_open = os.open
    calls = {"n": 0}

    def swapping_open(path, flags, *args, **kwargs):
        if str(path) == str(db) and calls["n"] == 0:
            calls["n"] += 1
            db.unlink()
            db.symlink_to(secret)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sqlite_copy.os, "open", swapping_open)
    dest = tmp_path / "dest"
    dest.mkdir()

    assert sqlite_copy.copy_sqlite_with_sidecars(db, dest, 10_000) is None
    assert not (dest / "swap.db").exists() or b"TOP SECRET" not in (dest / "swap.db").read_bytes()


def test_symlinked_brain_directory_is_not_followed(home, tmp_path):
    outside = tmp_path / "outside-brain"
    conv = outside / "conv-link" / ".system_generated" / "logs"
    conv.mkdir(parents=True)
    records = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q1"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a1"},
        {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q2"},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a2"},
    ]
    (conv / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    base = home / ".gemini" / "antigravity"
    base.mkdir(parents=True)
    (base / "brain").symlink_to(outside, target_is_directory=True)

    _, sessions = _extract(home)

    assert sessions == []


def test_same_conversation_in_two_roots_is_yielded_once(home):
    for variant in ("antigravity", "antigravity-cli"):
        conv = home / ".gemini" / variant / "brain" / "conv-dup" / ".system_generated" / "logs"
        conv.mkdir(parents=True)
        records = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q1"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a1"},
            {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q2"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a2"},
        ]
        (conv / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    extractor, sessions = _extract(home)

    assert [s.session_id for s in sessions] == ["conv-dup"]
    assert extractor.discovery.get("duplicate_sessions") == 1


def test_pb_files_are_never_listed_or_opened(home, monkeypatch):
    import os

    base = home / ".gemini" / "antigravity"
    conv = base / "brain" / "conv-pb" / ".system_generated" / "logs"
    conv.mkdir(parents=True)
    records = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q1"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a1"},
        {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "q2"},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a2"},
    ]
    (conv / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    pb_dir = base / "conversations"
    pb_dir.mkdir()
    (pb_dir / "conv-pb.pb").write_bytes(b"\x00encrypted")

    touched: list[str] = []
    real_scandir, real_listdir = os.scandir, os.listdir

    def spy_scandir(path="."):
        touched.append(str(path))
        return real_scandir(path)

    def spy_listdir(path="."):
        touched.append(str(path))
        return real_listdir(path)

    # The generic home discovery walks the whole tree (and so lists every directory it
    # meets); this test pins the extractor's own behaviour, so it is replaced.
    monkeypatch.setattr("lore.extractors.antigravity.discover_home_marker_paths", lambda marker: [])
    monkeypatch.setattr(os, "scandir", spy_scandir)
    monkeypatch.setattr(os, "listdir", spy_listdir)

    _, sessions = _extract(home)

    assert len(sessions) == 1
    assert not [p for p in touched if "conversations" in p]
