"""Origin policy (#197, B1.3): automated sessions are stored but hidden by default.

Sessions whose ``origin`` is ``exec``, ``cron`` or ``subagent`` bypass the user-prompt
floor, stay in the archive, and are hidden from list/search views and LLM stages unless
``include_automated`` (CLI ``--include-automated``, web ``automated=1``, MCP argument) is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest

import lore.llm
import lore_cli
from lore.core.models import AUTOMATED_ORIGINS, Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor
from lore.extractors.claude import ClaudeCodeExtractor
from lore.extractors.gemini import GeminiCLIExtractor
from lore.interfaces import mcp, web, web_data
from lore.llm import LLMConfig
from lore.llm.base import LLMProvider, LLMResponse
from lore.search.engine import SearchEngine
from lore.services import extraction
from lore.services.index import (
    InvalidFlagError,
    apply_deleted_filter,
    automated_hidden_line,
    filter_automated,
    filter_automated_payload,
    parse_bool_flag,
    search_index,
)
from lore.storage.writer import v2_db_path

T0 = datetime(2026, 5, 1, 10, 0, 0)
SHARED_WORD = "quokkaterm"


def _session(
    sid: str,
    prompts: int,
    origin: str = "unknown",
    *,
    thread: Optional[str] = None,
    project: str = "/proj/alpha",
) -> UnifiedSession:
    messages: list[UnifiedMessage] = []
    for i in range(prompts):
        messages.append(
            UnifiedMessage(role=Role.USER, content=f"{SHARED_WORD} {sid} ask {i}", timestamp=T0)
        )
        messages.append(
            UnifiedMessage(role=Role.ASSISTANT, content=f"answer {i} for {sid}", timestamp=T0)
        )
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=T0,
        last_updated=T0,
        messages=messages,
        project_path=project,
        thread_id=thread,
        origin=origin,
    )


class FixtureExtractor(BaseExtractor):
    """Yields the given sessions through the real quality filter."""

    def __init__(self, sessions: list[UnifiedSession], **limits: int) -> None:
        self.sessions = sessions
        for name, value in limits.items():
            setattr(self, name, value)

    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        for session in self.sessions:
            if self.should_import_session(session):
                yield session


@pytest.fixture(autouse=True)
def _production_prompt_floor(monkeypatch):
    # conftest relaxes the floor to 1; the policy is only visible with the real 3.
    monkeypatch.setattr(BaseExtractor, "MIN_USER_PROMPTS", 3)
    monkeypatch.delenv("LORE_MIN_USER_PROMPTS", raising=False)
    monkeypatch.delenv("LORE_IMPORT_PROFILE", raising=False)


# --- extractor-level policy -----------------------------------------------------------


def test_automated_origins_constant():
    assert AUTOMATED_ORIGINS == ("exec", "cron", "subagent")


@pytest.mark.parametrize("origin", ["exec", "cron", "subagent"])
def test_automated_origin_bypasses_prompt_floor(origin):
    extractor = FixtureExtractor([_session("a", 1, origin)])
    assert [s.session_id for s in extractor.extract_sessions()] == ["a"]
    assert extractor.skip_counts == {}


@pytest.mark.parametrize("origin", ["interactive", "unknown"])
def test_other_origins_below_prompt_floor_are_still_dropped(origin):
    extractor = FixtureExtractor([_session("a", 1, origin), _session("b", 3, origin)])
    assert [s.session_id for s in extractor.extract_sessions()] == ["b"]
    assert extractor.skip_counts == {"too_few_user_prompts": 1}


def test_automated_origin_still_honours_message_and_content_floor():
    tiny = _session("tiny", 1, "exec")
    tiny.messages = tiny.messages[:1]
    tiny.messages[0].content = "x"
    ok = _session("ok", 1, "exec")
    ok.messages[0].content = "a request that is long enough to pass the content floor " * 2
    extractor = FixtureExtractor([tiny, ok], MIN_MESSAGES=3, MIN_CONTENT_SIZE=50)
    assert [s.session_id for s in extractor.extract_sessions()] == ["ok"]
    assert extractor.skip_counts == {"too_little_content": 1}


def test_bypass_is_not_counted_for_a_session_the_content_floor_drops():
    tiny = _session("tiny", 1, "exec")
    tiny.messages = tiny.messages[:1]
    tiny.messages[0].content = "x"
    extractor = FixtureExtractor([tiny], MIN_MESSAGES=2, MIN_CONTENT_SIZE=50)
    assert list(extractor.extract_sessions()) == []
    assert extractor.skip_counts == {"too_little_content": 1}
    assert extractor.discovery.get("automated_kept", 0) == 0


def test_bypass_is_counted_in_discovery_not_in_skips():
    extractor = FixtureExtractor(
        [_session("a", 1, "exec"), _session("b", 3, "exec"), _session("c", 1, "unknown")]
    )
    assert [s.session_id for s in extractor.extract_sessions()] == ["a", "b"]
    assert extractor.discovery.get("automated_kept") == 1
    assert extractor.skip_counts == {"too_few_user_prompts": 1}


# --- origin assignment ----------------------------------------------------------------


def _write_lines(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _gemini_msgs(count: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(count):
        out.append(
            {
                "id": f"u{i}",
                "timestamp": f"2026-05-01T10:{2 * i:02d}:00.000Z",
                "type": "user",
                "content": f"question {i}",
            }
        )
        out.append(
            {
                "id": f"g{i}",
                "timestamp": f"2026-05-01T10:{2 * i + 1:02d}:00.000Z",
                "type": "gemini",
                "content": f"answer {i}",
            }
        )
    return out


def _gemini_header(sid: str, kind: str) -> dict[str, Any]:
    return {
        "sessionId": sid,
        "projectHash": "f" * 64,
        "startTime": "2026-05-01T10:00:00.000Z",
        "lastUpdated": "2026-05-01T10:30:00.000Z",
        "kind": kind,
    }


def _gemini_home(tmp_path: Path, monkeypatch, prompts_sub: int) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_PROJECT_ROOTS", raising=False)
    chats = tmp_path / ".gemini" / "tmp" / "demo" / "chats"
    parent = "22222222-aaaa-bbbb-cccc-000000000002"
    _write_lines(
        chats / f"session-2026-05-01T10-00-{parent[:8]}.jsonl",
        [_gemini_header(parent, "main"), {"$set": {"messages": _gemini_msgs(3)}}],
    )
    _write_lines(
        chats / "session-2026-05-01T11-00-hdr00001.jsonl",
        [
            _gemini_header("hdr0001", "subagent"),
            {"$set": {"messages": _gemini_msgs(prompts_sub)}},
        ],
    )
    _write_lines(
        chats / parent / "sub0001.jsonl",
        [_gemini_header("sub0001", "subagent"), {"$set": {"messages": _gemini_msgs(3)}}],
    )
    return tmp_path


def test_gemini_subagent_sessions_get_origin_subagent(tmp_path, monkeypatch):
    _gemini_home(tmp_path, monkeypatch, prompts_sub=3)
    sessions = {s.session_id: s for s in GeminiCLIExtractor().extract_sessions()}
    origins = {sid: s.origin for sid, s in sessions.items()}
    assert origins["hdr0001"] == "subagent"
    assert origins["22222222-aaaa-bbbb-cccc-000000000002:sub0001"] == "subagent"
    main = next(s for sid, s in sessions.items() if s.origin != "subagent")
    assert main.origin == "unknown"


def test_gemini_subagent_below_floor_is_not_newly_imported(tmp_path, monkeypatch):
    _gemini_home(tmp_path, monkeypatch, prompts_sub=1)
    extractor = GeminiCLIExtractor()
    ids = {s.session_id for s in extractor.extract_sessions()}
    assert "hdr0001" not in ids
    assert extractor.skip_counts.get("too_few_user_prompts") == 1


def _claude_records(sid: str, prompts: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for i in range(prompts):
        records.append(
            {
                "type": "user",
                "timestamp": f"2026-05-01T10:{2 * i:02d}:00",
                "sessionId": sid,
                "message": {"role": "user", "content": f"question {i}"},
                "uuid": f"u{i}",
            }
        )
        records.append(
            {
                "type": "assistant",
                "timestamp": f"2026-05-01T10:{2 * i + 1:02d}:00",
                "sessionId": sid,
                "message": {"role": "assistant", "content": f"answer {i}"},
                "uuid": f"a{i}",
            }
        )
    return records


def test_claude_subagent_gets_origin_subagent_and_short_ones_stay_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-home-user-app"
    _write_lines(project / "main1.jsonl", _claude_records("main1", 3))
    _write_lines(project / "main1" / "subagents" / "agent-big.jsonl", _claude_records("x", 3))
    _write_lines(project / "main1" / "subagents" / "agent-tiny.jsonl", _claude_records("y", 1))
    extractor = ClaudeCodeExtractor()
    by_id = {s.session_id: s for s in extractor.extract_sessions()}
    assert set(by_id) == {"main1", "main1:agent-big"}
    assert by_id["main1:agent-big"].origin == "subagent"
    assert by_id["main1"].origin == "unknown"


def test_claude_subagent_records_its_parent_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-home-user-app"
    _write_lines(project / "main1.jsonl", _claude_records("main1", 3))
    _write_lines(project / "main1" / "subagents" / "agent-big.jsonl", _claude_records("x", 3))
    by_id = {s.session_id: s for s in ClaudeCodeExtractor().extract_sessions()}
    sub = by_id["main1:agent-big"]
    assert (sub.parent_session_id, sub.relation) == ("main1", "subagent")
    assert sub.parent_session_id in by_id
    assert (by_id["main1"].parent_session_id, by_id["main1"].relation) == (None, None)


def test_gemini_subagent_records_its_parent_session(tmp_path, monkeypatch):
    _gemini_home(tmp_path, monkeypatch, prompts_sub=3)
    sessions = {s.session_id: s for s in GeminiCLIExtractor().extract_sessions()}
    parent = "22222222-aaaa-bbbb-cccc-000000000002"
    sub = sessions[f"{parent}:sub0001"]
    assert (sub.parent_session_id, sub.relation) == (parent, "subagent")
    assert parent in sessions
    assert (sessions[parent].parent_session_id, sessions[parent].relation) == (None, None)
    # A header-only subagent has no parent directory, so no parent is invented.
    assert sessions["hdr0001"].parent_session_id is None
    assert sessions["hdr0001"].relation is None


def _gemini_main(chats: Path, name: str, sid: Optional[str]) -> None:
    header = _gemini_header(sid or "unused", "main")
    if sid is None:
        del header["sessionId"]
    _write_lines(chats / f"{name}.jsonl", [header, {"$set": {"messages": _gemini_msgs(3)}}])


def _gemini_sub(chats: Path, directory: str, name: str) -> None:
    _write_lines(
        chats / directory / f"{name}.jsonl",
        [_gemini_header(name, "subagent"), {"$set": {"messages": _gemini_msgs(3)}}],
    )


def _gemini_relations(tmp_path: Path, monkeypatch) -> dict[str, tuple[Any, Any]]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_PROJECT_ROOTS", raising=False)
    sessions = GeminiCLIExtractor().extract_sessions()
    return {s.session_id: (s.parent_session_id, s.relation) for s in sessions}


def test_gemini_subagent_parent_is_the_stem_when_the_main_file_has_no_session_id(
    tmp_path, monkeypatch
):
    chats = tmp_path / ".gemini" / "tmp" / "demo" / "chats"
    _gemini_main(chats, "session-2026-05-01T10-00-abcd1234", None)
    _gemini_sub(chats, "session-2026-05-01T10-00-abcd1234", "sub0001")
    _gemini_sub(chats, "other-dir", "sub0002")
    found = _gemini_relations(tmp_path, monkeypatch)
    main = "session-2026-05-01T10-00-abcd1234"
    assert found[main] == (None, None)
    assert found[f"{main}:sub0001"] == (main, "subagent")
    # The directory name matches neither a header sessionId nor a main file stem.
    assert found["other-dir:sub0002"] == (None, None)


def test_gemini_subagent_parent_is_the_deduped_session_id(tmp_path, monkeypatch):
    dup = "33333333-aaaa-bbbb-cccc-000000000003"
    first = tmp_path / ".gemini" / "tmp" / "alpha" / "chats"
    second = tmp_path / ".gemini" / "tmp" / "beta" / "chats"
    _gemini_main(first, "session-first", dup)
    _gemini_main(second, "session-second", dup)
    _gemini_sub(first, dup, "subA")
    _gemini_sub(second, dup, "subB")
    found = _gemini_relations(tmp_path, monkeypatch)
    assert found[dup] == (None, None)
    assert found["session-second"] == (None, None)
    assert found[f"{dup}:subA"] == (dup, "subagent")
    # The second main file lost the id to the first one and is stored under its stem.
    assert found[f"{dup}:subB"] == ("session-second", "subagent")


def test_gemini_subagent_without_a_main_file_has_no_parent(tmp_path, monkeypatch):
    chats = tmp_path / ".gemini" / "tmp" / "demo" / "chats"
    _gemini_sub(chats, "44444444-aaaa-bbbb-cccc-000000000004", "orphan")
    found = _gemini_relations(tmp_path, monkeypatch)
    assert found == {"44444444-aaaa-bbbb-cccc-000000000004:orphan": (None, None)}


def test_gemini_peek_reads_the_session_id_of_an_unparsed_main_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    chats = tmp_path / ".gemini" / "tmp" / "demo" / "chats"
    _gemini_main(chats, "session-a", "peek-id-a")
    _gemini_main(chats, "session-b", None)
    (chats / "session-c.json").write_text(
        json.dumps({"sessionId": "peek-id-c", "messages": []}), encoding="utf-8"
    )
    # Nothing was parsed in this run: the ids come from the file headers.
    ids = GeminiCLIExtractor()._main_session_ids(chats)
    assert ids == {"peek-id-a": "peek-id-a", "session-b": "session-b", "peek-id-c": "peek-id-c"}


def test_parser_versions_rise_for_origin_assignment():
    assert ClaudeCodeExtractor.PARSER_VERSION >= 3
    assert GeminiCLIExtractor.PARSER_VERSION >= 4


# --- the central filter ---------------------------------------------------------------


def test_filter_automated_dicts_and_objects():
    rows = [{"id": "a", "origin": "exec"}, {"id": "b", "origin": "interactive"}, {"id": "c"}]
    assert [r["id"] for r in filter_automated(rows, False)] == ["b", "c"]
    assert filter_automated(rows, True) is rows
    objs = [_session("a", 1, "cron"), _session("b", 1, "unknown")]
    assert [s.session_id for s in filter_automated(objs, False)] == ["b"]


def test_payload_filter_is_a_noop_without_automated_sessions():
    payload = {"sessions": [{"id": "a", "origin": "interactive"}, {"id": "b"}], "stats": {}}
    assert filter_automated_payload(payload, False) is payload


def test_payload_filter_recomputes_stats_and_reports_hidden():
    payload = {
        "sessions": [
            {"id": "a", "tool": "codex", "project": "p", "messages": 4, "origin": "exec"},
            {"id": "b", "tool": "codex", "project": "p", "messages": 6},
        ],
        "stats": {"total_sessions": 2},
    }
    out = filter_automated_payload(payload, False)
    assert [s["id"] for s in out["sessions"]] == ["b"]
    assert out["stats"]["total_sessions"] == 1
    assert out["stats"]["total_messages"] == 6
    assert out["automated_hidden"] == 1
    assert len(payload["sessions"]) == 2  # cached payload is never mutated


def test_deleted_and_automated_filters_are_order_independent():
    def payload() -> dict:
        return {
            "sessions": [
                {"id": "a", "tool": "t", "messages": 1, "origin": "subagent"},
                {"id": "b", "tool": "t", "messages": 2},
                {"id": "c", "tool": "t", "messages": 4},
            ],
            "stats": {},
        }

    one = filter_automated_payload(apply_deleted_filter(payload(), {"b"}), False)
    two = apply_deleted_filter(filter_automated_payload(payload(), False), {"b"})
    assert [s["id"] for s in one["sessions"]] == [s["id"] for s in two["sessions"]] == ["c"]
    assert one["stats"] == two["stats"]


def test_hidden_line_wording():
    assert automated_hidden_line(0) == ""
    assert automated_hidden_line(1) == "1 automated session hidden"
    assert automated_hidden_line(12) == "12 automated sessions hidden"


# --- store, search, web, MCP, CLI -----------------------------------------------------

VISIBLE = {"inter-one", "inter-two", "plain-one"}
AUTOMATED = {"exec-sess1", "sub-sess1"}


def _fixture_sessions() -> list[UnifiedSession]:
    return [
        _session("inter-one", 3, "interactive", thread="thread-main"),
        _session("inter-two", 4, "interactive", project="/proj/beta"),
        _session("plain-one", 3, "unknown"),
        _session("short-inter", 1, "interactive"),  # dropped by the floor
        _session("short-plain", 2, "unknown"),  # dropped by the floor
        _session(
            "exec-sess1", 1, "exec", project="/proj/gamma", thread="thread-exec"
        ),  # kept, below the floor
        _session("sub-sess1", 1, "subagent", thread="thread-main"),
    ]


def _point_everything_at(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.json"
    for module in (web, web_data, mcp):
        monkeypatch.setattr(module, "INDEX_PATH", index_path, raising=False)
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", tmp_path / "deleted.json")
    monkeypatch.setattr(web, "DELETED_SESSIONS_PATH", tmp_path / "deleted.json", raising=False)
    web.clear_index_cache()
    web._filtered_sorted_session_ids.cache_clear()


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    extractor = FixtureExtractor(_fixture_sessions())
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    _point_everything_at(tmp_path, monkeypatch)
    return tmp_path


def _db_origins(root: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(v2_db_path(root)))
    try:
        return dict(conn.execute("SELECT id, origin FROM sessions").fetchall())
    finally:
        conn.close()


def _ids(results: list[dict]) -> set[str]:
    return {r["session"]["id"] for r in results}


def test_automated_sessions_are_stored_and_floor_drops_stay_dropped(store):
    origins = _db_origins(store)
    assert set(origins) == VISIBLE | AUTOMATED
    assert origins["exec-sess1"] == "exec"
    assert origins["sub-sess1"] == "subagent"
    index = json.loads((store / "index.json").read_text(encoding="utf-8"))
    assert {s["id"] for s in index["sessions"]} == VISIBLE | AUTOMATED


def test_search_hides_automated_by_default_and_shows_them_with_the_switch(store):
    index_path = store / "index.json"
    assert _ids(search_index(index_path, SHARED_WORD, set())) == VISIBLE
    assert _ids(search_index(index_path, SHARED_WORD, set(), include_automated=True)) == (
        VISIBLE | AUTOMATED
    )


def test_search_legacy_fallback_hides_automated(store, monkeypatch):
    monkeypatch.setenv("LORE_USE_V2", "0")
    web.clear_index_cache()
    index_path = store / "index.json"
    assert _ids(search_index(index_path, SHARED_WORD, set())) == VISIBLE
    assert _ids(search_index(index_path, SHARED_WORD, set(), include_automated=True)) == (
        VISIBLE | AUTOMATED
    )


def test_storage_search_filters_in_sql(store):
    from lore.storage import search_sessions

    assert _ids(search_sessions(store, SHARED_WORD)) == VISIBLE
    assert _ids(search_sessions(store, SHARED_WORD, include_automated=True)) == (
        VISIBLE | AUTOMATED
    )


def test_hybrid_semantic_path_filters_automated(store, monkeypatch):
    import lore.storage as storage

    def fake_semantic(output_dir, query, **kwargs):
        include = kwargs.get("include_automated", False)
        ids = sorted(VISIBLE | AUTOMATED) if include else sorted(VISIBLE)
        return [
            {
                "session": {"id": sid, "tool": "claude-code", "origin": _db_origins(store)[sid]},
                "score": 1.0,
            }
            for sid in ids
        ]

    monkeypatch.setenv("LORE_HYBRID_SEARCH", "1")
    monkeypatch.setattr(storage, "semantic_search_sessions", fake_semantic)
    index_path = store / "index.json"
    assert _ids(search_index(index_path, SHARED_WORD, set())) == VISIBLE
    assert _ids(search_index(index_path, SHARED_WORD, set(), include_automated=True)) == (
        VISIBLE | AUTOMATED
    )


def _page_ids(body: str) -> set[str]:
    known = VISIBLE | AUTOMATED | {"short-inter", "a", "b", "c"}
    return {sid for sid in known if f"/session/{sid}?" in body}


def test_web_sessions_list_toggle_round_trip(store):
    client = web.app.test_client()
    default = client.get("/sessions").get_data(as_text=True)
    assert _page_ids(default) == VISIBLE
    assert "2 automated sessions hidden" in default
    assert 'name="automated"' in default
    assert 'name="automated" value="1" data-auto-submit checked' not in default

    shown = client.get("/sessions?automated=1").get_data(as_text=True)
    assert _page_ids(shown) == VISIBLE | AUTOMATED
    assert "automated sessions hidden" not in shown
    assert 'name="automated" value="1" data-auto-submit checked' in shown
    assert 'data-automated="1"' in shown

    again = client.get("/sessions").get_data(as_text=True)
    assert _page_ids(again) == VISIBLE


def test_web_rows_fragment_honours_the_switch(store):
    client = web.app.test_client()
    assert _page_ids(client.get("/sessions/rows?page=1").get_data(as_text=True)) == VISIBLE
    shown = client.get("/sessions/rows?page=1&automated=1").get_data(as_text=True)
    assert _page_ids(shown) == VISIBLE | AUTOMATED


def test_web_dashboard_counts_and_hidden_line(store):
    client = web.app.test_client()
    body = client.get("/").get_data(as_text=True)
    assert "2 automated sessions hidden" in body
    assert "exec-sess1" not in body
    shown = client.get("/?automated=1").get_data(as_text=True)
    assert "automated sessions hidden" not in shown


def test_web_hidden_count_ignores_tombstoned_automated_sessions(store):
    (store / "deleted.json").write_text(
        json.dumps({"session_ids": ["exec-sess1"]}), encoding="utf-8"
    )
    web.clear_index_cache()
    web._filtered_sorted_session_ids.cache_clear()
    body = web.app.test_client().get("/sessions").get_data(as_text=True)
    assert "1 automated session hidden" in body
    assert _page_ids(body) == VISIBLE
    shown = web.app.test_client().get("/sessions?automated=1").get_data(as_text=True)
    assert _page_ids(shown) == VISIBLE | {"sub-sess1"}


def test_web_json_api_lists_and_search(store):
    client = web.app.test_client()
    listed = client.get("/api/v1/sessions").get_json()
    assert {s["id"] for s in listed["sessions"]} == VISIBLE
    listed_all = client.get("/api/v1/sessions?automated=1").get_json()
    assert {s["id"] for s in listed_all["sessions"]} == VISIBLE | AUTOMATED

    found = client.get(f"/api/v1/search?q={SHARED_WORD}").get_json()
    assert {r["id"] for r in found["results"]} == VISIBLE
    found_all = client.get(f"/api/v1/search?q={SHARED_WORD}&automated=1").get_json()
    assert {r["id"] for r in found_all["results"]} == VISIBLE | AUTOMATED

    summary = client.get("/api/v1/index/summary").get_json()
    assert summary["total_sessions"] == len(VISIBLE)
    assert summary["automated_hidden"] == 2
    summary_all = client.get("/api/v1/index/summary?automated=1").get_json()
    assert summary_all["total_sessions"] == len(VISIBLE | AUTOMATED)


def test_web_projects_threads_and_stats_hide_automated(store):
    client = web.app.test_client()
    projects = client.get("/api/v1/projects").get_json()
    assert "/proj/gamma" not in json.dumps(projects)
    projects_all = client.get("/api/v1/projects?automated=1").get_json()
    assert "/proj/gamma" in json.dumps(projects_all)
    assert "2 automated sessions hidden" in client.get("/stats").get_data(as_text=True)
    assert "/proj/gamma" not in client.get("/projects").get_data(as_text=True)
    assert "/proj/gamma" in client.get("/projects?automated=1").get_data(as_text=True)


def test_web_direct_links_by_id_keep_working(store):
    client = web.app.test_client()
    assert client.get("/session/exec-sess1").status_code == 200
    assert client.get("/api/v1/sessions/exec-sess1").status_code == 200
    thread = client.get("/api/v1/threads/thread-main")
    assert thread.status_code == 200


def _mcp_call(name: str, arguments: dict) -> dict:
    server = mcp.create_server()
    return json.loads(asyncio.run(server.tools[name]["handler"](arguments)))


def test_mcp_tools_hide_automated_and_accept_the_switch(store):
    listed = _mcp_call("list_sessions", {"limit": 50})
    assert {s["id"] for s in listed["sessions"]} == VISIBLE
    listed_all = _mcp_call("list_sessions", {"limit": 50, "include_automated": True})
    assert {s["id"] for s in listed_all["sessions"]} == VISIBLE | AUTOMATED

    recent = _mcp_call("list_recent_sessions", {"limit": 50})
    assert {s["id"] for s in recent["sessions"]} == VISIBLE
    recent_all = _mcp_call("list_recent_sessions", {"limit": 50, "include_automated": True})
    assert {s["id"] for s in recent_all["sessions"]} == VISIBLE | AUTOMATED

    found = _mcp_call("search_history", {"query": SHARED_WORD, "limit": 50})
    assert {r["id"] for r in found["results"]} == VISIBLE
    found_all = _mcp_call(
        "search_history", {"query": SHARED_WORD, "limit": 50, "include_automated": True}
    )
    assert {r["id"] for r in found_all["results"]} == VISIBLE | AUTOMATED

    projects = _mcp_call("list_projects", {})
    assert "/proj/gamma" not in json.dumps(projects)


def test_mcp_get_session_and_thread_by_id_work_for_hidden_sessions(store):
    assert _mcp_call("get_session", {"session_id": "exec-sess1"})["id"] == "exec-sess1"
    assert "error" not in _mcp_call("get_thread", {"thread_id": "thread-main"})
    only_automated = _mcp_call("get_thread", {"thread_id": "thread-exec"})
    assert "error" not in only_automated


def test_default_views_are_unchanged_for_interactive_only_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    extractor = FixtureExtractor(
        [_session("a", 3, "interactive"), _session("b", 3, "unknown"), _session("c", 1, "unknown")]
    )
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    _point_everything_at(tmp_path, monkeypatch)

    assert _ids(search_index(tmp_path / "index.json", SHARED_WORD, set())) == {"a", "b"}
    assert search_index(tmp_path / "index.json", SHARED_WORD, set()) == search_index(
        tmp_path / "index.json", SHARED_WORD, set(), include_automated=True
    )
    client = web.app.test_client()
    default = client.get("/sessions").get_data(as_text=True)
    assert _page_ids(default) == {"a", "b"}
    assert "automated session" not in default
    assert _mcp_call("list_sessions", {}) == _mcp_call("list_sessions", {"include_automated": True})
    index = web.load_index()
    assert filter_automated_payload(index, False) is index


def _cli_args(store: Path, **extra: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "output_dir": str(store),
        "tool": None,
        "project": None,
        "since": None,
        "limit": None,
        "offset": None,
        "format": "json",
        "query": SHARED_WORD,
    }
    base.update(extra)
    return argparse.Namespace(**base)


def test_cli_search_hides_automated_by_default(store, capsys):
    lore_cli.cmd_search(_cli_args(store))
    out = capsys.readouterr().out
    assert "Found 3 results" in out
    lore_cli.cmd_search(_cli_args(store, include_automated=True))
    assert "Found 5 results" in capsys.readouterr().out


def test_cli_list_hides_automated_and_reports_the_count(store, monkeypatch, capsys):
    extractor = FixtureExtractor(_fixture_sessions())
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    lore_cli.cmd_list(_cli_args(store))
    out = capsys.readouterr().out
    listed = json.loads(out[: out.rindex("]") + 1])
    assert {row["session_id"] for row in listed} == VISIBLE
    assert "2 automated sessions hidden" in out

    lore_cli.cmd_list(_cli_args(store, include_automated=True))
    out = capsys.readouterr().out
    listed = json.loads(out[: out.rindex("]") + 1])
    assert {row["session_id"] for row in listed} == VISIBLE | AUTOMATED
    assert "automated sessions hidden" not in out


def test_cli_stats_threads_and_digest_hide_automated(store, capsys):
    lore_cli.cmd_stats(_cli_args(store))
    out = capsys.readouterr().out
    assert "Total Sessions: 3" in out
    assert "2 automated sessions hidden" in out
    lore_cli.cmd_stats(_cli_args(store, include_automated=True))
    assert "Total Sessions: 5" in capsys.readouterr().out


class _RecordingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(LLMConfig(provider="fake", model="fake-model"))
        self.prompts: list[str] = []

    def generate(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        self.prompts.append(prompt)
        payload = {
            "topics": ["t"],
            "summary": "s",
            "key_points": [],
            "code_snippets": [],
            "decisions": [],
            "tools_used": [],
        }
        return LLMResponse(content=json.dumps(payload), model="fake-model")

    def generate_batch(self, prompts, system_prompt=None):
        return [self.generate(p) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self) -> dict[str, Any]:
        return {"model": "fake-model"}


def _knowledge_ids(store: Path) -> set[str]:
    from lore.storage import list_session_knowledge

    return {row["session_id"] for row in list_session_knowledge(store)}


def test_knowledge_candidates_exclude_automated_unless_switched(store, monkeypatch):
    provider = _RecordingProvider()
    monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
    args = _cli_args(store, provider="gemini", model=None, output=None, limit=50, allow_cloud=True)

    assert lore_cli.cmd_knowledge(args) == 0
    assert _knowledge_ids(store) == VISIBLE

    args = _cli_args(
        store,
        provider="gemini",
        model=None,
        output=None,
        limit=50,
        include_automated=True,
        allow_cloud=True,
    )
    assert lore_cli.cmd_knowledge(args) == 0
    assert _knowledge_ids(store) == VISIBLE | AUTOMATED

    # A stored extraction of an automated session never reaches the default knowledge base.
    args = _cli_args(store, provider="gemini", model=None, output=None, limit=50, allow_cloud=True)
    assert lore_cli.cmd_knowledge(args) == 0
    kb = json.loads((store / "knowledge" / "knowledge_base.json").read_text(encoding="utf-8"))
    assert kb["total_entries"] == len(VISIBLE)


def test_prune_never_touches_automated_sessions_unless_asked(store, capsys):
    args = _cli_args(store, max_prompts=3, dry_run=True)
    lore_cli.cmd_prune(args)
    assert "Found 2 sessions" in capsys.readouterr().out
    lore_cli.cmd_prune(_cli_args(store, max_prompts=3, dry_run=True, include_automated=True))
    assert "Found 4 sessions" in capsys.readouterr().out


def test_single_tool_sync_merge_keeps_automated_entries_below_the_floor():
    entries = [
        {"id": "exec-low", "tool": "claude-code", "prompts": 1, "messages": 2, "origin": "exec"},
        {"id": "plain-low", "tool": "claude-code", "prompts": 1, "messages": 2},
    ]
    merged = lore_cli._merge_sessions_with_existing_index([], entries)
    assert {s.session_id for s in merged} == {"exec-low"}
    assert merged[0].origin == "exec"


def test_cli_format_with_an_explicit_id_reaches_a_hidden_session(store, monkeypatch, capsys):
    provider = _RecordingProvider()
    monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
    args = _cli_args(
        store,
        provider="gemini",
        model=None,
        limit=5,
        session_id="exec-sess1",
        output_dir=str(store),
        allow_cloud=True,
    )
    lore_cli.cmd_format(args)
    assert "Formatting 1 sessions" in capsys.readouterr().out


def test_cli_parser_exposes_the_switch():
    parser = lore_cli.build_parser()
    for argv in (
        ["list"],
        ["search", "q"],
        ["stats"],
        ["threads"],
        ["digest"],
        ["knowledge"],
        ["format"],
        ["analyze"],
        ["prune"],
        ["rules"],
        ["generate-titles"],
    ):
        assert parser.parse_args([*argv, "--include-automated"]).include_automated is True
        assert parser.parse_args(argv).include_automated is False


# --- discovery recording is best effort (B1.2 review) ---------------------------------


def _fail_connect(monkeypatch) -> None:
    import lore.services.discovery as discovery

    def boom(*args: Any, **kwargs: Any):
        raise sqlite3.OperationalError("database is locked")

    stub = SimpleNamespace(connect=boom, Error=sqlite3.Error)
    monkeypatch.setattr(discovery, "sqlite3", stub)


def test_build_search_index_records_discovery_into_store_meta(tmp_path, monkeypatch):
    extractor = FixtureExtractor([_session("a", 3, "unknown")])
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    index_path = tmp_path / "index.json"
    extraction.build_search_index(tmp_path, index_path, incremental=False)
    conn = sqlite3.connect(str(v2_db_path(tmp_path)))
    try:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key = ?", (f"discovery:{Tool.CLAUDE_CODE.value}",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0])["sessions"] == 1


def test_build_search_index_survives_a_failing_discovery_write(tmp_path, monkeypatch):
    extractor = FixtureExtractor([_session("a", 3, "unknown")])
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    index_path = tmp_path / "index.json"
    extraction.build_search_index(tmp_path, index_path, incremental=False)  # creates the store
    _fail_connect(monkeypatch)
    extraction.build_search_index(tmp_path, index_path, incremental=False)
    assert json.loads(index_path.read_text(encoding="utf-8"))["sessions"]


def _export_args(out: Path) -> argparse.Namespace:
    return argparse.Namespace(output_dir=str(out), tool=None, project=None, full=True)


def test_cmd_export_records_discovery_into_store_meta(tmp_path, monkeypatch):
    extractor = FixtureExtractor([_session("a", 3, "unknown")])
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    lore_cli.cmd_export(_export_args(tmp_path))
    conn = sqlite3.connect(str(v2_db_path(tmp_path)))
    try:
        keys = {row[0] for row in conn.execute("SELECT key FROM store_meta")}
    finally:
        conn.close()
    assert f"discovery:{Tool.CLAUDE_CODE.value}" in keys


def test_cmd_export_survives_a_failing_discovery_write(tmp_path, monkeypatch):
    extractor = FixtureExtractor([_session("a", 3, "unknown")])
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    lore_cli.cmd_export(_export_args(tmp_path))  # creates the store
    _fail_connect(monkeypatch)
    lore_cli.cmd_export(_export_args(tmp_path))
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["sessions"]


# --- review fixes (B1.3 round 2) ------------------------------------------------------


def _build_store(tmp_path: Path, monkeypatch, sessions: list[UnifiedSession]) -> Path:
    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    extractor = FixtureExtractor(sessions)
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    _point_everything_at(tmp_path, monkeypatch)
    return tmp_path


def test_tag_filter_keeps_tagged_automated_sessions_visible(store, monkeypatch):
    # The keyword tag filter reads ``keywords``, which only the JSON index carries.
    monkeypatch.setenv("LORE_USE_V2", "0")
    web.clear_index_cache()
    index = json.loads((store / "index.json").read_text(encoding="utf-8"))
    entry = next(s for s in index["sessions"] if s["id"] == "exec-sess1")
    assert SHARED_WORD in entry["keywords"]

    client = web.app.test_client()
    tagged = client.get(f"/sessions?tag={SHARED_WORD}").get_data(as_text=True)
    assert "exec-sess1" in _page_ids(tagged)
    rows = client.get(f"/sessions/rows?tag={SHARED_WORD}&page=1").get_data(as_text=True)
    assert "exec-sess1" in _page_ids(rows)
    projects = client.get(f"/projects?tag={SHARED_WORD}").get_data(as_text=True)
    assert "/proj/gamma" in projects
    # Without a tag the default still hides them.
    assert "exec-sess1" not in _page_ids(client.get("/sessions").get_data(as_text=True))


def test_filter_results_are_memoized_on_the_source_identity():
    sessions = [{"id": "a", "origin": "exec"}, {"id": "b"}]
    payload = {"sessions": sessions, "stats": {}}
    first = filter_automated_payload(payload, False)
    assert filter_automated_payload(payload, False) is first
    assert first["sessions"] is filter_automated(sessions, False)
    assert filter_automated(sessions, False) is filter_automated(sessions, False)
    assert filter_automated_payload(payload, True) is payload
    other = {"sessions": list(sessions), "stats": {}}
    renewed = filter_automated_payload(other, False)
    assert renewed is not first
    assert filter_automated_payload(other, False) is renewed
    # Memoizing another payload leaves the first one's entry intact.
    assert filter_automated_payload(payload, False) is first


def test_visible_index_is_one_object_per_request_so_identity_memos_hit(store):
    with web.app.test_request_context("/sessions"):
        first = web._visible_index()
        assert first is web._visible_index()
        assert first["automated_hidden"] == 2
    from lore.interfaces.web_services import thread_overview_by_id

    with web.app.test_request_context("/threads"):
        sessions = web._visible_index()["sessions"]
        assert thread_overview_by_id(sessions) is thread_overview_by_id(
            web._visible_index()["sessions"]
        )


def test_costs_cover_all_sessions_and_name_the_automated_share(monkeypatch):
    index = {
        "sessions": [
            {"id": "a", "tool": "codex", "project": "/p", "created": "2026-06-01", "tokens": 100},
            {
                "id": "b",
                "tool": "codex",
                "project": "/p",
                "created": "2026-06-01",
                "tokens": 40,
                "origin": "subagent",
            },
            {
                "id": "c",
                "tool": "codex",
                "project": "/p",
                "created": "2026-06-01",
                "tokens": 0,
                "origin": "exec",
            },
        ]
    }
    monkeypatch.setattr(web, "load_index", lambda: index)
    payload = web.app.test_client().get("/api/stats/costs").get_json()
    assert payload["total_tokens"] == 140
    assert payload["session_count"] == 2
    assert payload["automated_included"] == 1
    assert payload["automated_included_line"] == "includes 1 automated session"

    index["sessions"] = index["sessions"][:1]
    payload = web.app.test_client().get("/api/stats/costs").get_json()
    assert payload["automated_included"] == 0
    assert payload["automated_included_line"] == ""


def test_thread_detail_hides_automated_children_and_says_so(store):
    client = web.app.test_client()
    default = client.get("/api/v1/threads/thread-main").get_json()
    assert {s["id"] for s in default["timeline"]} == {"inter-one"}
    assert default["automated_hidden"] == 1
    shown = client.get("/api/v1/threads/thread-main?automated=1").get_json()
    assert {s["id"] for s in shown["timeline"]} == {"inter-one", "sub-sess1"}
    assert "automated_hidden" not in shown

    page = client.get("/thread/thread-main").get_data(as_text=True)
    assert "1 automated session hidden" in page
    assert "automated=1" in page
    assert "sub-sess1" not in page
    assert "1 automated session hidden" not in client.get(
        "/thread/thread-main?automated=1"
    ).get_data(as_text=True)


def test_mcp_get_thread_hides_children_and_reports_them(store):
    default = _mcp_call("get_thread", {"thread_id": "thread-main"})
    assert default["thread"]["session_count"] == 1
    assert default["automated_hidden"] == 1
    assert default["automated_hidden_line"] == "1 automated session hidden"
    shown = _mcp_call("get_thread", {"thread_id": "thread-main", "include_automated": True})
    assert shown["thread"]["session_count"] == 2
    assert "automated_hidden" not in shown
    # A thread of automated sessions only is reachable and reports nothing hidden.
    only = _mcp_call("get_thread", {"thread_id": "thread-exec"})
    assert "automated_hidden" not in only


def test_hidden_counts_are_scoped_to_the_active_filters(store):
    gamma = _mcp_call("list_sessions", {"project": "/proj/gamma"})
    assert gamma["automated_hidden"] == 1
    beta = _mcp_call("list_sessions", {"project": "/proj/beta"})
    assert "automated_hidden" not in beta
    everything = _mcp_call("list_sessions", {})
    assert everything["automated_hidden"] == 2

    client = web.app.test_client()
    scoped = client.get("/sessions?tool=claude-code").get_data(as_text=True)
    assert "2 automated sessions hidden" in scoped
    other = client.get("/sessions?tool=codex").get_data(as_text=True)
    assert "automated session" not in other
    assert "automated session" not in client.get("/projects?tool=codex").get_data(as_text=True)


def _dated(sid: str, origin: str, day: int) -> UnifiedSession:
    session = _session(sid, 3, origin)
    session.last_updated = datetime(2026, 5, day, 10, 0, 0)
    session.created_at = datetime(2026, 5, day, 9, 0, 0)
    return session


@pytest.fixture
def recency_store(tmp_path, monkeypatch) -> Path:
    sessions = [
        _dated("old-a", "interactive", 1),
        _dated("old-b", "unknown", 2),
        _dated("new-exec", "exec", 5),
        _dated("new-sub", "subagent", 6),
    ]
    return _build_store(tmp_path, monkeypatch, sessions)


def test_listing_limits_apply_after_the_origin_filter(recency_store):
    listed = _mcp_call("list_sessions", {"limit": 2})
    assert [s["id"] for s in listed["sessions"]] == ["old-b", "old-a"]
    recent = _mcp_call("list_recent_sessions", {"limit": 2})
    assert [s["id"] for s in recent["sessions"]] == ["old-b", "old-a"]
    api = web.app.test_client().get("/api/v1/sessions?limit=2").get_json()
    assert [s["id"] for s in api["sessions"]] == ["old-b", "old-a"]
    everything = _mcp_call("list_recent_sessions", {"limit": 2, "include_automated": True})
    assert [s["id"] for s in everything["sessions"]] == ["new-sub", "new-exec"]


def test_summary_on_the_legacy_index_hides_automated(store, monkeypatch):
    from lore.services.index import load_index_summary

    monkeypatch.setenv("LORE_USE_V2", "0")
    web.clear_index_cache()
    index_path = store / "index.json"
    summary = load_index_summary(index_path, set())
    assert summary["total_sessions"] == len(VISIBLE)
    assert summary["automated_hidden"] == 2
    assert summary["by_tool"] == {"claude-code": len(VISIBLE)}
    everything = load_index_summary(index_path, set(), include_automated=True)
    assert everything["total_sessions"] == len(VISIBLE | AUTOMATED)
    assert everything["automated_hidden"] == 0
    with_tombstone = load_index_summary(index_path, {"inter-one"})
    assert with_tombstone["total_sessions"] == len(VISIBLE) - 1
    assert with_tombstone["automated_hidden"] == 2
    served = web.app.test_client().get("/api/v1/index/summary").get_json()
    assert served["automated_hidden"] == 2


def test_prune_run_leaves_automated_sessions_alone(store, monkeypatch, capsys):
    extractor = FixtureExtractor(_fixture_sessions())
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    lore_cli.cmd_prune(_cli_args(store, max_prompts=3, dry_run=False))
    assert "Pruned 2 sessions" in capsys.readouterr().out
    ignored = set(json.loads((store / "ignored.json").read_text(encoding="utf-8")))
    assert ignored == {"inter-one", "plain-one"}
    index = json.loads((store / "index.json").read_text(encoding="utf-8"))
    assert AUTOMATED <= {s["id"] for s in index["sessions"]}


def test_legacy_search_filters_before_the_row_limit(tmp_path, monkeypatch):
    def flooder(sid: str, origin: str, text: str) -> UnifiedSession:
        session = _session(sid, 3, origin)
        for message in session.messages:
            message.content = text
        return session

    noisy = [flooder(f"auto-{i:02d}", "subagent", f"{SHARED_WORD} " * 8) for i in range(60)]
    quiet = [
        flooder(f"human-{i}", "interactive", f"{SHARED_WORD} " + "filler " * 60) for i in range(3)
    ]
    _build_store(tmp_path, monkeypatch, noisy + quiet)
    assert (tmp_path / "index.sqlite").exists()
    monkeypatch.setenv("LORE_USE_V2", "0")
    web.clear_index_cache()
    found = search_index(tmp_path / "index.json", SHARED_WORD, set())
    assert _ids(found) == {"human-0", "human-1", "human-2"}
    # bm25 scores are negative; the keyword fallback scan would score them positive.
    assert all(r["score"] < 0 for r in found)
    everything = search_index(tmp_path / "index.json", SHARED_WORD, set(), include_automated=True)
    assert len(everything) == 50


def test_semantic_search_widens_k_when_the_neighbourhood_is_automated(tmp_path, monkeypatch):
    import struct

    from lore.storage import semantic_search_sessions, write_sessions
    from lore.storage.embeddings import EMBEDDING_DIM, sqlite_vec_available
    from lore.storage.schema import ensure_session_vec_table, initialise

    if not sqlite_vec_available():
        pytest.skip("needs sqlite-vec")
    automated = [_session(f"auto-{i:02d}", 1, "subagent") for i in range(10)]
    visible = [_session("human-a", 3, "interactive"), _session("human-b", 3, "interactive")]
    monkeypatch.setattr("lore.storage.embeddings.embed_text", lambda text: None)
    write_sessions(v2_db_path(tmp_path), automated + visible)
    conn = initialise(v2_db_path(tmp_path))
    try:
        assert ensure_session_vec_table(conn)
        conn.execute("DELETE FROM session_embeddings")

        def pack(x: float, y: float) -> bytes:
            values = [x, y] + [0.0] * (EMBEDDING_DIM - 2)
            return struct.pack(f"<{EMBEDDING_DIM}f", *values)

        rows = [(s.session_id, pack(1.0, 0.01 * i)) for i, s in enumerate(automated)]
        # Human neighbours must be *farther* than the automated ones (0.00-0.09) so the
        # widening matters, but still inside the calibrated distance cutoff (0.88): these
        # sit at 0.706 and 0.837. Vectors orthogonal to the query (0.0, 1.0) would be at
        # 1.414 and get dropped before fusion (#92), which is not what this test is about.
        rows += [("human-a", pack(0.75, 0.66)), ("human-b", pack(0.65, 0.76))]
        for session_id, vec in rows:
            conn.execute(
                "INSERT INTO session_embeddings(session_id, model, embedding) VALUES (?, ?, ?)",
                (session_id, "test", vec),
            )
    finally:
        conn.close()
    query = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    monkeypatch.setattr("lore.storage.embeddings.embed_text", lambda text: query)

    hits = semantic_search_sessions(tmp_path, "anything", limit=2)  # first k is 8: all automated
    assert [h["session"]["id"] for h in hits] == ["human-a", "human-b"]
    everything = semantic_search_sessions(tmp_path, "anything", limit=2, include_automated=True)
    assert [h["session"]["id"] for h in everything] == ["auto-00", "auto-01"]


# --- B1.4: strict flags, caps, thread by id, show URL ---------------------------------


def _mcp_raw(name: str, arguments: dict) -> str:
    server = mcp.create_server()
    return asyncio.run(server.tools[name]["handler"](arguments))


MCP_FLAG_TOOLS = {
    "list_sessions": {"limit": 50},
    "list_recent_sessions": {"limit": 50},
    "search_history": {"query": SHARED_WORD, "limit": 50},
    "list_projects": {},
    "get_thread": {"thread_id": "thread-main"},
}


def _mcp_shows_automated(name: str, args: dict[str, Any]) -> bool:
    payload = json.loads(_mcp_raw(name, args))
    if name == "search_history":
        return bool({r["id"] for r in payload["results"]} & AUTOMATED)
    if name == "list_projects":
        return "/proj/gamma" in json.dumps(payload)
    if name == "get_thread":
        return payload["thread"]["session_count"] == 2
    return bool({s["id"] for s in payload["sessions"]} & AUTOMATED)


@pytest.mark.parametrize("name", sorted(MCP_FLAG_TOOLS))
@pytest.mark.parametrize("value", ["false", False, 0, "no", "FALSE", ""])
def test_mcp_false_flag_values_keep_automated_hidden(store, name, value):
    assert not _mcp_shows_automated(name, {**MCP_FLAG_TOOLS[name], "include_automated": value})


@pytest.mark.parametrize("name", sorted(MCP_FLAG_TOOLS))
@pytest.mark.parametrize("value", ["true", True, 1, "yes", "1"])
def test_mcp_true_flag_values_show_automated(store, name, value):
    assert _mcp_shows_automated(name, {**MCP_FLAG_TOOLS[name], "include_automated": value})


@pytest.mark.parametrize("name", sorted(MCP_FLAG_TOOLS))
@pytest.mark.parametrize("value", ["maybe", ["true"], {"a": 1}, 2, 1.5, -1])
def test_mcp_unknown_flag_values_are_an_error_string(store, name, value):
    text = _mcp_raw(name, {**MCP_FLAG_TOOLS[name], "include_automated": value})
    assert text == "Invalid include_automated parameter."


def test_parse_bool_flag_defaults_and_errors():
    assert parse_bool_flag(None) is False
    assert parse_bool_flag(None, default=True) is True
    assert parse_bool_flag(" Yes ") is True
    with pytest.raises(InvalidFlagError):
        parse_bool_flag("maybe", name="automated")
    assert issubclass(InvalidFlagError, ValueError)


@pytest.mark.parametrize("value", ["on", "ON", " on "])
def test_parse_bool_flag_accepts_on(value):
    assert parse_bool_flag(value) is True


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_parse_bool_flag_accepts_off(value):
    assert parse_bool_flag(value) is False


@pytest.mark.parametrize("value", [1.0, 0.0, 1.5, 2, -1])
def test_parse_bool_flag_rejects_floats_and_other_numbers(value):
    with pytest.raises(InvalidFlagError):
        parse_bool_flag(value)


def test_parse_bool_flag_accepts_only_int_zero_and_one():
    assert parse_bool_flag(1) is True
    assert parse_bool_flag(0) is False


def test_web_repeated_automated_parameter_takes_the_first_value(store):
    client = web.app.test_client()
    first_hides = client.get("/api/v1/sessions?automated=0&automated=1").get_json()
    assert {s["id"] for s in first_hides["sessions"]} == VISIBLE
    first_shows = client.get("/api/v1/sessions?automated=1&automated=0").get_json()
    assert {s["id"] for s in first_shows["sessions"]} == VISIBLE | AUTOMATED


@pytest.mark.parametrize("path", ["/sessions", "/api/v1/sessions", "/api/v1/threads/thread-main"])
def test_web_rejects_an_unknown_automated_value_with_400_json(store, path):
    client = web.app.test_client()
    response = client.get(f"{path}?automated=maybe")
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid automated parameter."}


def test_web_false_automated_value_keeps_them_hidden(store):
    client = web.app.test_client()
    for value in ("false", "0", "no", ""):
        body = client.get(f"/api/v1/sessions?automated={value}").get_json()
        assert {s["id"] for s in body["sessions"]} == VISIBLE, value


def test_search_widening_is_capped(tmp_path, monkeypatch, caplog):
    """A table of mostly automated rows stops at the scan cap and returns what it has."""
    from lore.search import engine as engine_module

    index_path = tmp_path / "index.json"
    automated = [f"auto-{i:04d}" for i in range(2600)]
    index = {
        "sessions": [{"id": sid, "origin": "subagent"} for sid in automated],
        "search_index": {},
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")
    conn = sqlite3.connect(index_path.with_suffix(".sqlite"))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, tool TEXT, project TEXT, thread_id TEXT, "
        "title TEXT, created TEXT, updated TEXT, messages INTEGER, export_path TEXT)"
    )
    conn.execute("CREATE VIRTUAL TABLE sessions_fts USING fts5(id, title, project, tool, text)")
    # The one interactive row matches weakest, so it ranks behind every automated row.
    rows = [(sid, f"{SHARED_WORD} " * 5) for sid in automated]
    rows.append(("human-1", f"{SHARED_WORD} " + "filler " * 50))
    for sid, text in rows:
        conn.execute("INSERT INTO sessions(id, tool, messages) VALUES (?, 'codex', 1)", (sid,))
        conn.execute(
            "INSERT INTO sessions_fts(id, title, project, tool, text) VALUES (?, '', '', '', ?)",
            (sid, text),
        )
    conn.commit()
    conn.close()

    limits: list[int] = []
    real_connect = sqlite3.connect

    class Spy:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def execute(self, sql: str, params: list) -> sqlite3.Cursor:
            limits.append(params[-1])
            return self.inner.execute(sql, params)

        def close(self) -> None:
            self.inner.close()

    monkeypatch.setattr(engine_module.sqlite3, "connect", lambda p: Spy(real_connect(p)))
    engine = SearchEngine(index_path)
    with caplog.at_level(logging.WARNING, logger="lore.search.engine"):
        found = engine.search(SHARED_WORD, include_automated=False)

    assert found == []
    assert limits == [50, 200, 800, engine_module.SQLITE_MAX_SCANNED_ROWS]
    assert engine.last_search_truncated is True
    warnings = [r for r in caplog.records if r.name == "lore.search.engine"]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert SHARED_WORD not in warnings[0].getMessage()
    assert "2000" in warnings[0].getMessage()
    # A later search that is not cut short clears the flag.
    engine.search(SHARED_WORD, include_automated=True)
    assert engine.last_search_truncated is False


def test_thread_of_only_automated_sessions_is_returned_by_id(store):
    client = web.app.test_client()
    api = client.get("/api/v1/threads/thread-exec")
    assert api.status_code == 200
    assert {s["id"] for s in api.get_json()["timeline"]} == {"exec-sess1"}
    assert client.get("/thread/thread-exec").status_code == 200
    only = _mcp_call("get_thread", {"thread_id": "thread-exec"})
    assert only["thread"]["session_count"] == 1
    assert "automated_hidden" not in only


def test_show_url_escapes_a_hostile_query_string(store):
    client = web.app.test_client()
    hostile = '"><script>alert(1)</script>'
    body = client.get("/stats", query_string={"x": hostile}).get_data(as_text=True)
    assert "2 automated sessions hidden" in body
    assert "<script>alert(1)" not in body
    link = re.search(r'<a href="([^"]*)"[^>]*>show</a>', body)
    assert link is not None
    assert link.group(1).startswith("/stats?")
    assert "automated=1" in link.group(1)
    assert "<" not in link.group(1) and ">" not in link.group(1)


def test_index_entry_to_session_keeps_the_contract_fields():
    entry = {
        "id": "sub-1",
        "tool": "claude-code",
        "origin": "subagent",
        "parent_session_id": "main-1",
        "relation": "subagent",
        "source_version": "2.1",
        "parser_version": 3,
    }
    session = lore_cli._index_entry_to_session(entry)
    assert (session.origin, session.parent_session_id, session.relation) == (
        "subagent",
        "main-1",
        "subagent",
    )
    assert (session.source_version, session.parser_version) == ("2.1", 3)
    bare = lore_cli._index_entry_to_session({"id": "old", "tool": "claude-code"})
    assert (bare.parent_session_id, bare.relation, bare.parser_version) == (None, None, 0)
