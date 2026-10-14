"""Parser-version reuse invalidation (#197, B1.1b).

An unchanged source file is reused without parsing. A session indexed by an
older parser (``parser_version`` below the extractor's ``PARSER_VERSION``) must
be parsed again once, so fields added to an extractor later reach old sessions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import (
    DEFAULT_REEXTRACT_BUDGET,
    REEXTRACT_BUDGET_ENV,
    BaseExtractor,
    reextract_budget_from_env,
)
from lore.extractors.gemini import GeminiCLIExtractor
from lore.interfaces.api_payloads import (
    serialize_index_session_summary,
    serialize_live_session,
)
from lore.services import extraction
from lore.storage import load_index_v2, open_connection
from lore.storage.writer import (
    _refresh_complete_session,
    _write_reused_entry,
    _write_reused_full_session,
    v2_db_path,
    write_sessions,
)

T0 = datetime(2026, 1, 1)
CONTRACT_KEYS = ("origin", "parent_session_id", "relation", "source_version", "parser_version")


class FakeExtractor(BaseExtractor):
    """One ``*.src`` file yields ``per_file`` sessions; counts every parse."""

    PARSER_VERSION = 1

    def __init__(self, src: Path, per_file: int = 1, origin: str = "interactive") -> None:
        self.src = src
        self.per_file = per_file
        self.origin = origin
        self.parse_calls: list[str] = []

    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        # Like the real extractors, decide every source before the first yield:
        # the caller reads ``skipped_sources`` when the first session arrives.
        todo = [p for p in sorted(self.src.glob("*.src")) if not self._source_unchanged(p)]
        for path in todo:
            self.parse_calls.append(path.name)
            for i in range(self.per_file):
                yield UnifiedSession(
                    tool=self.tool,
                    session_id=f"{path.stem}-{i}",
                    created_at=T0,
                    last_updated=datetime(2026, 1, 2),
                    project_path="/p",
                    title=f"Fake session {path.stem} {i}",
                    source_path=str(path),
                    origin=self.origin,
                    parent_session_id="parent-1" if self.origin == "subagent" else None,
                    relation="subagent" if self.origin == "subagent" else None,
                    source_version="9.9",
                    messages=[
                        UnifiedMessage(role=Role.USER, content=f"q{i}", timestamp=T0),
                        UnifiedMessage(role=Role.ASSISTANT, content=f"a{i}", timestamp=T0),
                    ],
                )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.src").write_text("x", encoding="utf-8")
    extractor = FakeExtractor(src)
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    return tmp_path, src, extractor


def _export(tmp_path: Path) -> dict:
    out = tmp_path / "out"
    lore_cli.cmd_export(Namespace(output_dir=str(out), tool=None, project=None, full=False))
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def _set_stored_version(tmp_path: Path, version: int | None) -> None:
    path = tmp_path / "out" / "index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if version is None:
            entry.pop("parser_version", None)
        else:
            entry["parser_version"] = version
    path.write_text(json.dumps(index), encoding="utf-8")


def test_stale_parser_version_is_reextracted_despite_unchanged_mtime(env):
    tmp_path, _, extractor = env
    _export(tmp_path)
    assert extractor.parse_calls == ["one.src"]

    _set_stored_version(tmp_path, 0)
    extractor.parse_calls.clear()
    index = _export(tmp_path)

    assert extractor.parse_calls == ["one.src"]
    assert index["sessions"][0]["parser_version"] == 1


def test_index_without_parser_version_is_reextracted_once(env):
    """Entries written before migration 17 carry no key, which counts as 0."""
    tmp_path, _, extractor = env
    _export(tmp_path)
    _set_stored_version(tmp_path, None)
    extractor.parse_calls.clear()

    _export(tmp_path)

    assert extractor.parse_calls == ["one.src"]


def test_current_parser_version_is_reused_without_parsing(env):
    tmp_path, _, extractor = env
    first = _export(tmp_path)
    extractor.parse_calls.clear()

    second = _export(tmp_path)

    assert extractor.parse_calls == []
    assert [s["id"] for s in second["sessions"]] == [s["id"] for s in first["sessions"]]
    assert second["sessions"][0]["parser_version"] == 1


def test_multi_session_source_reextracts_when_any_session_is_stale(env):
    tmp_path, src, extractor = env
    extractor.per_file = 3
    _export(tmp_path)

    path = tmp_path / "out" / "index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    index["sessions"][1]["parser_version"] = 0  # one of three is stale
    path.write_text(json.dumps(index), encoding="utf-8")
    extractor.parse_calls.clear()
    refreshed = _export(tmp_path)

    assert extractor.parse_calls == ["one.src"]
    assert sorted(s["id"] for s in refreshed["sessions"]) == ["one-0", "one-1", "one-2"]
    assert {s["parser_version"] for s in refreshed["sessions"]} == {1}


def test_multi_session_source_is_fully_reused_when_all_are_current(env):
    tmp_path, _, extractor = env
    extractor.per_file = 3
    _export(tmp_path)
    extractor.parse_calls.clear()

    index = _export(tmp_path)

    assert extractor.parse_calls == []
    assert sorted(s["id"] for s in index["sessions"]) == ["one-0", "one-1", "one-2"]


def test_reextraction_is_idempotent(env):
    tmp_path, _, extractor = env
    _export(tmp_path)
    _set_stored_version(tmp_path, 0)

    _export(tmp_path)  # re-extracts once
    extractor.parse_calls.clear()
    _export(tmp_path)  # then reuses

    assert extractor.parse_calls == []


def test_fresh_values_win_on_reextraction_and_stored_survive_on_reuse(env):
    tmp_path, _, extractor = env
    first = _export(tmp_path)
    assert first["sessions"][0]["origin"] == "interactive"

    # Re-extracted: the stale source is parsed again and the new values land.
    extractor.origin = "subagent"
    _set_stored_version(tmp_path, 0)
    second = _export(tmp_path)
    entry = second["sessions"][0]
    assert (entry["origin"], entry["parent_session_id"], entry["relation"]) == (
        "subagent",
        "parent-1",
        "subagent",
    )
    assert entry["source_version"] == "9.9"

    # Reused: the extractor would now report something else, but it is not asked.
    extractor.origin = "cron"
    extractor.parse_calls.clear()
    third = _export(tmp_path)
    assert extractor.parse_calls == []
    assert {k: third["sessions"][0][k] for k in CONTRACT_KEYS} == {
        k: entry[k] for k in CONTRACT_KEYS
    }

    conn = open_connection(v2_db_path(tmp_path / "out"))
    try:
        row = conn.execute(
            "SELECT origin, parent_session_id, relation, parser_version FROM sessions"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("subagent", "parent-1", "subagent", 1)


def _stored(tmp_path: Path, sid: str) -> tuple:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        return tuple(
            conn.execute(
                "SELECT origin, parent_session_id, relation, source_version, parser_version "
                "FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
        )
    finally:
        conn.close()


def _full_session(sid: str, **kwargs) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=sid,
        created_at=T0,
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="Writer title",
        messages=[UnifiedMessage(role=Role.USER, content="hello", timestamp=T0)],
        **kwargs,
    )


def test_reused_full_session_keeps_stored_contract_fields(tmp_path):
    stored = _full_session(
        "s1",
        origin="exec",
        parent_session_id="p0",
        relation="fork",
        parser_version=3,
        source_version="1.0",
    )
    write_sessions(v2_db_path(tmp_path), [stored])

    # Same parser version: the stored values survive a metadata-only refresh.
    again = _full_session("s1", origin="interactive", parser_version=3)
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        _write_reused_full_session(conn, again, "Writer title", {})
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "s1") == ("exec", "p0", "fork", "1.0", 3)

    # A newer parser version is a re-extraction: fresh values win.
    newer = _full_session("s1", origin="subagent", relation="subagent", parser_version=4)
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        _write_reused_full_session(conn, newer, "Writer title", {})
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "s1") == ("subagent", None, "subagent", None, 4)


def test_reused_entry_dict_keeps_or_receives_contract_fields(tmp_path):
    stored = _full_session("s2", origin="cron", relation="continuation", parser_version=2)
    write_sessions(v2_db_path(tmp_path), [stored])

    base = {
        "id": "s2",
        "tool": "codex",
        "title": "Writer title",
        "created": T0.isoformat(),
        "updated": T0.isoformat(),
        "messages": 1,
        "prompts": 1,
        "search_text": "hello",
    }
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        # Existing complete row: stored contract columns are not reset.
        _write_reused_entry(conn, {**base, "origin": "unknown", "parser_version": 0})
        # New metadata-only row: the entry's contract fields are stored.
        _write_reused_entry(
            conn,
            {
                **base,
                "id": "s3",
                "origin": "subagent",
                "parent_session_id": "s2",
                "relation": "subagent",
                "source_version": "2.0",
                "parser_version": 5,
            },
        )
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "s2") == ("cron", None, "continuation", None, 2)
    assert _stored(tmp_path, "s3") == ("subagent", "s2", "subagent", "2.0", 5)
    sessions = {s["id"]: s for s in load_index_v2(tmp_path)["sessions"]}
    assert sessions["s3"]["parser_version"] == 5
    assert sessions["s3"]["origin"] == "subagent"


def test_index_entries_and_payloads_expose_new_keys_additively(env):
    tmp_path, _, _ = env
    index = _export(tmp_path)
    entry = index["sessions"][0]
    for key in CONTRACT_KEYS:
        assert key in entry
    assert entry["parser_version"] == 1
    assert entry["origin"] == "interactive"
    assert entry["source_version"] == "9.9"

    summary = serialize_index_session_summary(entry)
    assert {k: summary[k] for k in CONTRACT_KEYS} == {k: entry[k] for k in CONTRACT_KEYS}
    assert summary["id"] == entry["id"]
    assert summary["message_count"] == entry["messages"]
    assert summary["export_path"] == entry["export_path"]

    legacy = serialize_index_session_summary({"id": "old", "messages": 2})
    assert legacy["origin"] == "unknown"
    assert legacy["parser_version"] == 0
    assert legacy["parent_session_id"] is None


def test_build_search_index_reextracts_a_stale_entry(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.src").write_text("x", encoding="utf-8")
    extractor = FakeExtractor(src)
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    index_path = tmp_path / "index.json"

    extraction.build_search_index(tmp_path, index_path, incremental=True)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["sessions"][0]["parser_version"] == 1

    payload["sessions"][0]["parser_version"] = 0
    payload["sessions"][0]["origin"] = "unknown"
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    progress: list[str] = []
    extraction.build_search_index(
        tmp_path, index_path, incremental=True, progress_callback=lambda p, m: progress.append(m)
    )
    refreshed = json.loads(index_path.read_text(encoding="utf-8"))["sessions"][0]
    assert refreshed["parser_version"] == 1
    assert refreshed["origin"] == "interactive"
    assert any("1 refreshed" in m for m in progress)

    progress.clear()
    extraction.build_search_index(
        tmp_path, index_path, incremental=True, progress_callback=lambda p, m: progress.append(m)
    )
    assert any("1 reused" in m for m in progress)


def test_explicit_parser_version_is_not_overwritten(tmp_path):
    class Explicit(FakeExtractor):
        PARSER_VERSION = 7

        def extract_sessions(self) -> Iterator[UnifiedSession]:
            for session in super().extract_sessions():
                session.parser_version = 4
                yield session

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.src").write_text("x", encoding="utf-8")
    (src / "b.src").write_text("x", encoding="utf-8")
    versions = [s.parser_version for s in Explicit(src).extract_sessions()]
    assert versions == [4, 4]
    assert [s.parser_version for s in FakeExtractor(src).extract_sessions()] == [1, 1]


def test_gemini_declares_parser_version_three():
    assert GeminiCLIExtractor.PARSER_VERSION == 3
    assert BaseExtractor.PARSER_VERSION == 1


# --- first-run re-extraction budget -------------------------------------------------


@pytest.fixture
def backlog(tmp_path, monkeypatch):
    """Ten indexed sources, all stored with parser version 0; a new extractor per run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "3")
    src = tmp_path / "src"
    src.mkdir()
    for i in range(10):
        (src / f"s{i:02d}.src").write_text("x", encoding="utf-8")
    runs: list[FakeExtractor] = []

    def make() -> list[FakeExtractor]:
        runs.append(FakeExtractor(src))
        return [runs[-1]]

    monkeypatch.setattr(lore_cli, "get_all_extractors", make)
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "-1")
    _export(tmp_path)  # a full first parse, unbudgeted
    _set_stored_version(tmp_path, 0)
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "3")
    runs.clear()
    return tmp_path, src, runs


def _stale_count(index: dict) -> int:
    return sum(1 for s in index["sessions"] if s["parser_version"] < 1)


def test_budget_drains_the_stale_backlog_over_several_runs(backlog):
    tmp_path, _, runs = backlog

    index = _export(tmp_path)
    assert len(runs[0].parse_calls) == 3
    # Reused-but-stale entries keep the old version; they are never stamped.
    assert _stale_count(index) == 7
    assert len(index["sessions"]) == 10

    index = _export(tmp_path)
    assert len(runs[1].parse_calls) == 3
    assert set(runs[1].parse_calls).isdisjoint(runs[0].parse_calls)
    assert _stale_count(index) == 4

    index = _export(tmp_path)
    assert len(runs[2].parse_calls) == 3
    assert _stale_count(index) == 1

    index = _export(tmp_path)
    assert len(runs[3].parse_calls) == 1
    assert _stale_count(index) == 0

    _export(tmp_path)
    assert runs[4].parse_calls == []


def test_budget_exhaustion_logs_one_info_line(backlog, caplog):
    tmp_path, _, _ = backlog
    with caplog.at_level(logging.INFO, logger="lore.extractors.base"):
        _export(tmp_path)
    lines = [r for r in caplog.records if "re-extract budget" in r.getMessage()]
    assert len(lines) == 1
    assert "spent" in lines[0].getMessage()


def test_changed_source_is_parsed_even_when_budget_is_spent(backlog):
    tmp_path, src, runs = backlog
    monkey_budget = os.environ
    monkey_budget[REEXTRACT_BUDGET_ENV] = "1"
    last = src / "s09.src"
    stat = last.stat()
    os.utime(last, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    index = _export(tmp_path)

    assert runs[0].parse_calls == ["s00.src", "s09.src"]
    assert _stale_count(index) == 8


def _origins(index: dict) -> dict[str, str]:
    return {s["id"]: s["origin"] for s in index["sessions"]}


def _untagged_run(monkeypatch, src: Path) -> None:
    """A first run by a parser that does not know about automated origins yet."""
    extractor = FakeExtractor(src, origin="unknown")
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])


def _tagging_run(monkeypatch, src: Path) -> FakeExtractor:
    """The next run uses a newer parser that tags these sources as subagents."""

    class Tagging(FakeExtractor):
        PARSER_VERSION = 2

    extractor = Tagging(src, origin="subagent")
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    return extractor


def test_unchanged_source_gets_the_origin_of_the_newer_parser(backlog, monkeypatch):
    tmp_path, src, _ = backlog
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "-1")
    _untagged_run(monkeypatch, src)
    index = _export(tmp_path)
    assert set(_origins(index).values()) == {"unknown"}
    extractor = _tagging_run(monkeypatch, src)

    index = _export(tmp_path)

    assert len(extractor.parse_calls) == 10  # nothing changed on disk, all re-extracted
    assert set(_origins(index).values()) == {"subagent"}
    assert {s["parser_version"] for s in index["sessions"]} == {2}


def test_budget_spent_run_keeps_the_old_origin_until_drained(backlog, monkeypatch):
    tmp_path, src, _ = backlog
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "-1")
    _untagged_run(monkeypatch, src)
    _export(tmp_path)
    extractor = _tagging_run(monkeypatch, src)
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "3")

    index = _export(tmp_path)

    assert len(extractor.parse_calls) == 3
    origins = _origins(index)
    assert sorted(origins.values()) == ["subagent"] * 3 + ["unknown"] * 7
    assert {sid for sid, o in origins.items() if o == "subagent"} == {
        Path(c).stem + "-0" for c in extractor.parse_calls
    }


def test_budget_default_and_invalid_values(monkeypatch):
    monkeypatch.delenv(REEXTRACT_BUDGET_ENV, raising=False)
    assert DEFAULT_REEXTRACT_BUDGET == 150
    assert reextract_budget_from_env() == 150
    for raw, expected in (("0", 150), ("abc", 150), ("", 150), ("7", 7), ("-1", None)):
        monkeypatch.setenv(REEXTRACT_BUDGET_ENV, raw)
        assert reextract_budget_from_env() == expected, raw


# --- stamping -------------------------------------------------------------------------


def _bare_session(sid: str = "b1") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=sid,
        created_at=T0,
        last_updated=T0,
        project_path="/p",
        messages=[UnifiedMessage(role=Role.USER, content="hi", timestamp=T0)],
    )


class Versioned(BaseExtractor):
    PARSER_VERSION = 3

    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        yield _bare_session("v1")

    def find_session_by_id(self, session_id: str) -> UnifiedSession | None:
        return _bare_session(session_id)


def test_sessions_are_stamped_with_the_subclass_parser_version():
    extractor = Versioned()
    assert [s.parser_version for s in extractor.extract_sessions()] == [3]
    assert BaseExtractor.PARSER_VERSION != Versioned.PARSER_VERSION


def test_live_lookup_and_payload_report_the_extractor_parser_version():
    session = Versioned().find_session_by_id("live-1")
    assert session is not None
    assert session.parser_version == 3
    assert serialize_live_session(session)["parser_version"] == 3


def test_gemini_session_is_stamped_with_its_parser_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_PROJECT_ROOTS", raising=False)
    chats = tmp_path / ".gemini" / "tmp" / "demo" / "chats"
    chats.mkdir(parents=True)
    messages = []
    for n in range(3):
        stamp = f"2026-05-01T10:{n * 2:02d}:00.000Z"
        messages.append({"id": f"u{n}", "timestamp": stamp, "type": "user", "content": f"q{n}"})
        messages.append({"id": f"g{n}", "timestamp": stamp, "type": "gemini", "content": f"a{n}"})
    records = [
        {
            "sessionId": "22222222-aaaa-bbbb-cccc-000000000002",
            "projectHash": "f" * 64,
            "startTime": "2026-05-01T10:00:00.000Z",
            "lastUpdated": "2026-05-01T10:30:00.000Z",
            "kind": "main",
        },
        {"$set": {"messages": messages}},
    ]
    (chats / "session-2026-05-01T10-00-bbbb2222.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    sessions = list(GeminiCLIExtractor().extract_sessions())
    assert sessions
    assert {s.parser_version for s in sessions} == {GeminiCLIExtractor.PARSER_VERSION}


def test_unstamped_session_gets_the_extractor_version_at_the_index_entry_point(
    tmp_path, monkeypatch
):
    """An extractor whose ``extract_sessions`` came from a mixin is never wrapped."""

    class Mixin:
        def extract_sessions(self) -> Iterator[UnifiedSession]:
            yield _bare_session("m1")

    class Mixed(Mixin, BaseExtractor):  # Mixin's method is not in Mixed.__dict__
        PARSER_VERSION = 4

        @property
        def tool(self) -> Tool:
            return Tool.CODEX

    assert [s.parser_version for s in Mixed().extract_sessions()] == [0]

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [Mixed()])
    exported = _export(tmp_path)
    assert exported["sessions"][0]["parser_version"] == 4

    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [Mixed()])
    index_path = tmp_path / "search" / "index.json"
    extraction.build_search_index(tmp_path / "search", index_path, incremental=False)
    built = json.loads(index_path.read_text(encoding="utf-8"))
    assert built["sessions"][0]["parser_version"] == 4


# --- writer guards --------------------------------------------------------------------


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(v2_db_path(tmp_path))


def test_reused_full_session_never_downgrades_the_stored_version(tmp_path):
    write_sessions(
        v2_db_path(tmp_path),
        [_full_session("d1", origin="exec", relation="fork", parser_version=5)],
    )
    older = _full_session("d1", origin="cron", parser_version=2)
    conn = _conn(tmp_path)
    try:
        _write_reused_full_session(conn, older, "Writer title", {})
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "d1") == ("exec", None, "fork", None, 5)


def test_incomplete_row_keeps_contract_values_against_default_entry(tmp_path):
    entry = {
        "id": "i1",
        "tool": "codex",
        "title": "Writer title",
        "created": T0.isoformat(),
        "updated": T0.isoformat(),
        "search_text": "hello",
    }
    conn = _conn(tmp_path)
    try:
        write_sessions(v2_db_path(tmp_path), [])
        _write_reused_entry(
            conn,
            {
                **entry,
                "origin": "cron",
                "relation": "fork",
                "source_version": "3",
                "parser_version": 2,
            },
        )
        # An older index: the seeded defaults carry no information.
        _write_reused_entry(
            conn,
            {
                **entry,
                "origin": "unknown",
                "parent_session_id": None,
                "relation": None,
                "source_version": None,
                "parser_version": 0,
            },
        )
        conn.commit()
        synced = conn.execute("SELECT messages_synced FROM sessions WHERE id = 'i1'").fetchone()
    finally:
        conn.close()
    assert synced == (0,)
    assert _stored(tmp_path, "i1") == ("cron", None, "fork", "3", 2)


def test_complete_row_at_defaults_is_healed_by_a_newer_entry(tmp_path):
    write_sessions(v2_db_path(tmp_path), [_full_session("h1")])
    assert _stored(tmp_path, "h1") == ("unknown", None, None, None, 0)
    entry = {
        "id": "h1",
        "tool": "codex",
        "title": "Writer title",
        "created": T0.isoformat(),
        "updated": T0.isoformat(),
        "origin": "subagent",
        "parent_session_id": "p",
        "relation": "subagent",
        "source_version": "1",
        "parser_version": 2,
    }
    conn = _conn(tmp_path)
    try:
        _refresh_complete_session(conn, entry, "Writer title", previous_title="Writer title")
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "h1") == ("subagent", "p", "subagent", "1", 2)

    older = {
        **entry,
        "origin": "unknown",
        "parent_session_id": None,
        "relation": None,
        "parser_version": 1,
    }
    conn = _conn(tmp_path)
    try:
        _refresh_complete_session(conn, older, "Writer title", previous_title="Writer title")
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "h1") == ("subagent", "p", "subagent", "1", 2)


def test_default_entry_never_wipes_values_stored_at_version_zero(tmp_path):
    write_sessions(v2_db_path(tmp_path), [_full_session("z1", origin="exec", relation="fork")])
    assert _stored(tmp_path, "z1") == ("exec", None, "fork", None, 0)
    entry = {
        "id": "z1",
        "tool": "codex",
        "title": "Writer title",
        "created": T0.isoformat(),
        "updated": T0.isoformat(),
        "origin": "unknown",
        "parser_version": 0,
    }
    conn = _conn(tmp_path)
    try:
        _refresh_complete_session(conn, entry, "Writer title", previous_title="Writer title")
        conn.commit()
    finally:
        conn.close()
    assert _stored(tmp_path, "z1") == ("exec", None, "fork", None, 0)


# --- stored version above the extractor's ---------------------------------------------


def test_stored_version_above_the_extractors_is_reused(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    source = src / "one.src"
    source.write_text("x", encoding="utf-8")
    extractor = FakeExtractor(src)
    extractor.known_source_mtimes = {str(source): source.stat().st_mtime_ns}
    extractor.known_source_parser_versions = {str(source): 5}
    assert extractor._source_unchanged(source) is True

    extractor.known_source_parser_versions = {str(source): 0}
    assert extractor._source_unchanged(source) is False
