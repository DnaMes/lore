"""Discovery telemetry: ``lore check --deep`` and the recorded last sync run (B1.2)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor
from lore.extractors.claude import ClaudeCodeExtractor
from lore.extractors.codex import CodexExtractor
from lore.extractors.cursor import CursorExtractor
from lore.extractors.gemini import GeminiCLIExtractor
from lore.extractors.vscode import VSCodeCopilotExtractor
from lore.services.discovery import record_discovery
from lore.storage.schema import initialise
from lore.storage.writer import v2_db_path

NOW = datetime(2026, 10, 14, 12, 0, 0)


class FixtureExtractor(BaseExtractor):
    """Reads ``*.jsonl`` from ``root``; a file holding ``unknown`` counts an unknown record."""

    MIN_USER_PROMPTS = 2

    def __init__(self, root: Path, pattern: str = "*.jsonl") -> None:
        self.root = root
        self.pattern = pattern

    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def is_available(self) -> bool:
        return self.root.exists()

    def source_description(self) -> str:
        return f"~/fixture/{self.pattern}"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        for path in sorted(self.root.glob(self.pattern)):
            self._count("files_matched")
            text = path.read_text(encoding="utf-8")
            if "unknown" in text:
                self._count("records_unknown")
            prompts = text.count("user")
            session = UnifiedSession(
                tool=Tool.CODEX,
                session_id=path.stem,
                created_at=NOW,
                last_updated=NOW,
                messages=[
                    UnifiedMessage(role=Role.USER, content="hello", timestamp=NOW)
                    for _ in range(prompts)
                ],
            )
            self._count("files_parsed")
            if self.should_import_session(session):
                yield session


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GEMINI_PROJECT_ROOTS", raising=False)
    # conftest relaxes the prompt floor; these tests need a real one to see skips.
    monkeypatch.setattr(BaseExtractor, "MIN_USER_PROMPTS", 2)
    return home


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _args(output_dir: Path, deep: bool) -> argparse.Namespace:
    return argparse.Namespace(output_dir=str(output_dir), deep=deep)


def _fixture_dir(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "good.jsonl").write_text("user\nuser\nuser\n", encoding="utf-8")
    (root / "weird.jsonl").write_text("user\nuser\nunknown record\n", encoding="utf-8")
    (root / "short.jsonl").write_text("user\n", encoding="utf-8")
    return root


def test_deep_warns_when_files_matched_but_no_sessions(monkeypatch, tmp_path, capsys):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "only.jsonl").write_text("user\n", encoding="utf-8")  # filtered: one prompt
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [FixtureExtractor(root)])

    lore_cli.cmd_check(_args(tmp_path / "out", deep=True))

    out = capsys.readouterr().out
    assert (
        "WARNING: codex: 1 source files matched ~/fixture/*.jsonl but 0 sessions were "
        "produced (stale glob or format change?)" in out
    )


def test_deep_warns_on_stale_glob_with_data_directory_present(monkeypatch, tmp_path, capsys):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "session.json").write_text("user\nuser\nuser\n", encoding="utf-8")  # glob wants jsonl
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [FixtureExtractor(root)])

    lore_cli.cmd_check(_args(tmp_path / "out", deep=True))

    out = capsys.readouterr().out
    assert "WARNING: codex: 0 source files matched ~/fixture/*.jsonl" in out
    assert "stale glob or format change?" in out


def test_deep_counts_match_fixture(monkeypatch, tmp_path, capsys):
    root = _fixture_dir(tmp_path)
    extractor = FixtureExtractor(root)
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])

    lore_cli.cmd_check(_args(tmp_path / "out", deep=True))

    assert extractor.discovery == {"files_matched": 3, "files_parsed": 3, "records_unknown": 1}
    assert extractor.skip_counts == {"too_few_user_prompts": 1}
    out = capsys.readouterr().out
    assert "Sources matched: 3" in out
    assert "Sessions parsed: 2" in out
    assert "Skipped: too_few_user_prompts=1" in out
    assert "Unknown records: 1" in out
    assert "Parsed files: 3" in out
    assert "WARNING" not in out


def test_deep_writes_nothing(home, tmp_path, capsys):
    claude = home / ".claude" / "projects" / "-tmp-demo"
    claude.mkdir(parents=True)
    lines = [json.dumps({"type": "user", "message": {"content": "hi"}, "sessionId": "s1"})] * 3
    (claude / "s1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    before = _snapshot(home)

    lore_cli.cmd_check(_args(out_dir, deep=True))

    assert _snapshot(home) == before
    assert not out_dir.exists()
    out = capsys.readouterr().out
    assert "claude-code" in out
    assert "Sessions parsed: 1" in out


def test_counters_reset_between_runs(tmp_path):
    extractor = FixtureExtractor(_fixture_dir(tmp_path))

    first = list(extractor.extract_sessions())
    snapshot = dict(extractor.discovery)
    second = list(extractor.extract_sessions())

    assert len(first) == len(second) == 2
    assert snapshot == {"files_matched": 3, "files_parsed": 3, "records_unknown": 1}
    assert extractor.discovery == snapshot


def test_plain_check_shows_last_recorded_run(monkeypatch, tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    initialise(v2_db_path(out_dir)).close()
    extractor = FixtureExtractor(_fixture_dir(tmp_path))
    list(extractor.extract_sessions())
    record_discovery(out_dir, [extractor])
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])

    lore_cli.cmd_check(_args(out_dir, deep=False))

    out = capsys.readouterr().out
    assert "Last sync" in out
    assert "3 sources matched, 3 files parsed, 2 sessions, 1 unknown records" in out
    assert "too_few_user_prompts=1" in out
    assert "Sources matched:" not in out  # no deep scan on a plain check


def test_plain_check_works_without_a_database(monkeypatch, tmp_path, capsys):
    out_dir = tmp_path / "out"
    extractor = FixtureExtractor(_fixture_dir(tmp_path))
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])

    lore_cli.cmd_check(_args(out_dir, deep=False))

    out = capsys.readouterr().out
    assert "codex" in out and "Available" in out
    assert "Last sync" not in out
    assert not out_dir.exists()


def test_record_discovery_never_creates_the_database(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    extractor = FixtureExtractor(_fixture_dir(tmp_path))
    list(extractor.extract_sessions())

    record_discovery(out_dir, [extractor])

    assert list(out_dir.iterdir()) == []


def test_record_discovery_stores_json_under_discovery_key(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    initialise(v2_db_path(out_dir)).close()
    extractor = FixtureExtractor(_fixture_dir(tmp_path))
    list(extractor.extract_sessions())

    record_discovery(out_dir, [extractor])

    conn = sqlite3.connect(str(v2_db_path(out_dir)))
    try:
        (raw,) = conn.execute(
            "SELECT value FROM store_meta WHERE key = 'discovery:codex'"
        ).fetchone()
    finally:
        conn.close()
    data: dict[str, Any] = json.loads(raw)
    assert data["files_matched"] == 3
    assert data["sessions"] == 2
    assert data["skip_counts"] == {"too_few_user_prompts": 1}
    assert data["recorded_at"]


def test_gemini_reports_counts(home):
    chats = home / ".gemini" / "tmp" / "demo" / "chats"
    chats.mkdir(parents=True)
    records: list[Any] = [
        {"sessionId": "g-1", "startTime": "2026-05-01T10:00:00.000Z", "kind": "main"},
    ]
    for i in range(3):
        records.append({"id": f"u{i}", "type": "user", "content": "q", "timestamp": "2026-05-01"})
    records.append({"id": "x1", "type": "mystery", "content": "?", "timestamp": "2026-05-01"})
    (chats / "session-a.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    (chats / "session-b.jsonl").write_text(json.dumps(records[0]) + "\n", encoding="utf-8")

    extractor = GeminiCLIExtractor()
    sessions = list(extractor.extract_sessions())

    assert len(sessions) == 1
    assert extractor.discovery["files_matched"] == 2
    assert extractor.discovery["files_parsed"] == 2
    assert extractor.discovery["records_unknown"] == 1
    assert extractor.record_counts["unknown_records"] == 1
    assert "~/.gemini/tmp" in extractor.source_description()


def test_claude_reports_counts(home):
    project = home / ".claude" / "projects" / "-tmp-demo"
    project.mkdir(parents=True)
    rows = [
        {
            "type": "user",
            "message": {"content": "hi"},
            "sessionId": "c-1",
            "timestamp": NOW.isoformat(),
        }
        for _ in range(3)
    ]
    rows.append({"type": "file-history-snapshot", "snapshot": {}})
    rows.append({"type": "bogus-record-type"})
    (project / "c-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    short = {**rows[0], "sessionId": "c-2"}
    (project / "c-2.jsonl").write_text(json.dumps(short) + "\n", encoding="utf-8")

    extractor = ClaudeCodeExtractor()
    sessions = list(extractor.extract_sessions())

    assert [s.session_id for s in sessions] == ["c-1"]
    assert extractor.discovery["files_matched"] == 2
    assert extractor.discovery["files_parsed"] == 2
    assert extractor.discovery["records_unknown"] == 1
    assert extractor.skip_counts == {"too_few_user_prompts": 1}
    assert "~/.claude/projects" in extractor.source_description()


def test_codex_reports_counts(home):
    day = home / ".codex" / "sessions" / "2026" / "10" / "14"
    day.mkdir(parents=True)
    meta = {"type": "session_meta", "timestamp": NOW.isoformat(), "payload": {"id": "x-1"}}
    msgs = [
        {
            "type": "message",
            "timestamp": NOW.isoformat(),
            "payload": {"role": "user", "content": "hi"},
        }
        for _ in range(3)
    ]
    event = {"type": "event_msg", "timestamp": NOW.isoformat(), "payload": {"type": "x"}}
    rows = [meta, *msgs, event]
    (day / "rollout-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    (day / "rollout-2.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())

    assert len(sessions) == 1
    assert extractor.discovery["files_matched"] == 2
    assert extractor.discovery["files_parsed"] == 2
    assert extractor.discovery["records_unknown"] == 1
    assert "~/.codex/sessions" in extractor.source_description()


def test_cursor_counts_only_real_parse_results(home):
    db = home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True)
    chat = {
        "tabs": [
            {"tabId": "t1", "bubbles": [{"type": "user", "text": f"hello {i}"} for i in range(2)]},
            {"tabId": "t2", "bubbles": []},  # nothing to parse: no session
        ]
    }
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES ('workbench.panel.aichat.view.aichat.chatdata', ?)",
        (json.dumps(chat),),
    )
    conn.commit()
    conn.close()

    extractor = CursorExtractor()
    sessions = list(extractor.extract_sessions())

    assert [s.session_id for s in sessions] == ["sidebar-t1"]
    assert extractor.discovery["files_matched"] == 2
    assert extractor.discovery["files_parsed"] == 1


def test_vscode_counts_each_unknown_record_once(home):
    chats = home / ".config" / "Code" / "User" / "workspaceStorage" / "w1" / "chatSessions"
    chats.mkdir(parents=True)
    rows: list[Any] = [
        {"kind": 0, "v": {"sessionId": "s1", "creationDate": 1_700_000_000_000}},
        {"kind": 9, "v": [{"kind": "other"}]},  # one unknown line, its parts are not walked
        {"kind": 2, "v": [{"requestId": "r1", "message": {"text": "hello there"}}]},
        {"kind": 2, "v": [{"kind": "toolInvocation"}, {"kind": "inlineReference"}]},  # two parts
        {"kind": 1, "k": ["x"], "v": 1},
    ]
    (chats / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    extractor = VSCodeCopilotExtractor()
    list(extractor.extract_sessions())

    assert extractor.discovery["files_matched"] == 1
    assert extractor.discovery["files_parsed"] == 1
    assert extractor.discovery["records_unknown"] == 3


def test_every_extractor_describes_its_sources_without_the_home_directory(home):
    from lore.extractors.factory import get_all_extractors

    for extractor in get_all_extractors():
        description = extractor.source_description()
        assert description and description != "" and str(home) not in description
        assert "~" in description, type(extractor).__name__
