"""Saved tags and bookmarks drive the list and search filters (#173).

The v2 store keeps user tags in ``session_tags``; the flat index keeps
LLM-generated ``keywords``. A tag filter has to see both, and it has to keep
working after a rebuild, a rewrite of the session and a consistent SQLite copy
(the primitive a backup bundle will use).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.interfaces.web_helpers import filter_sessions
from lore.interfaces.web_services import compute_top_tags
from lore.storage import (
    add_session_tag,
    all_tags_by_session,
    get_tags_for_sessions,
    list_all_tags,
)
from lore.storage.writer import write_sessions_safe
from lore.utils.sqlite_copy import copy_sqlite_with_sidecars


def _session(session_id: str) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/repo/demo",
        title=f"Session {session_id}",
        messages=[
            UnifiedMessage(Role.USER, "configure the xenotoken", datetime(2026, 1, 1)),
            UnifiedMessage(Role.ASSISTANT, "done", datetime(2026, 1, 1, 0, 1)),
        ],
    )


def _index_entry(session: UnifiedSession, keywords: list[str] | None = None) -> dict:
    """The index-shaped dict the filters see."""
    return {
        "id": session.session_id,
        "tool": session.tool.value,
        "title": session.title,
        "created": session.created_at.isoformat(),
        "updated": session.last_updated.isoformat(),
        "project": session.project_path,
        "keywords": list(keywords or []),
        "messages": len(session.messages),
        "prompts": 1,
    }


@pytest.fixture()
def archive(tmp_path):
    """A v2 store with two sessions, one of them tagged by the user."""
    sessions = [_session("tagged"), _session("plain")]
    write_sessions_safe(tmp_path, sessions)
    add_session_tag(tmp_path, "tagged", "bookmark")
    add_session_tag(tmp_path, "tagged", "release-check")
    return tmp_path


def test_all_tags_by_session_returns_every_saved_tag(archive):
    assert all_tags_by_session(archive) == {
        "tagged": ["bookmark", "release-check"],
    }
    assert list_all_tags(archive) == {"bookmark": 1, "release-check": 1}


def test_web_filter_matches_a_saved_tag(archive):
    entries = [_index_entry(_session("tagged")), _index_entry(_session("plain"))]
    kept = filter_sessions(entries, tag="bookmark", user_tags=all_tags_by_session(archive))
    assert [entry["id"] for entry in kept] == ["tagged"]


def test_web_filter_is_case_insensitive_for_saved_tags(archive):
    entries = [_index_entry(_session("tagged"))]
    kept = filter_sessions(entries, tag="BookMark", user_tags=all_tags_by_session(archive))
    assert [entry["id"] for entry in kept] == ["tagged"]


def test_web_filter_still_matches_index_keywords():
    """Existing behaviour: LLM keywords remain selectable without any saved tag."""
    entries = [_index_entry(_session("keyworded"), keywords=["sqlite"])]
    kept = filter_sessions(entries, tag="sqlite", user_tags={})
    assert [entry["id"] for entry in kept] == ["keyworded"]


def test_web_filter_drops_untagged_sessions(archive):
    entries = [_index_entry(_session("plain"))]
    assert filter_sessions(entries, tag="bookmark", user_tags=all_tags_by_session(archive)) == []


def test_dropdown_offers_saved_tags_even_without_keywords(archive):
    entries = [_index_entry(_session("tagged")), _index_entry(_session("plain"))]
    tags = compute_top_tags(entries, limit=12, user_tag_counts=list_all_tags(archive))
    assert tags[:2] == ["bookmark", "release-check"]


def test_saved_tags_survive_a_session_rewrite(archive):
    """A sync rewrites the session rows; the tags are keyed separately."""
    write_sessions_safe(archive, [_session("tagged"), _session("plain")])
    assert get_tags_for_sessions(archive, ["tagged"]) == {"tagged": ["bookmark", "release-check"]}


def test_saved_tags_survive_an_index_rebuild(archive):
    IndexBuilder(archive).build_index([_session("tagged"), _session("plain")], {})
    assert (archive / "index.json").exists()
    assert get_tags_for_sessions(archive, ["tagged"])["tagged"] == ["bookmark", "release-check"]


def test_saved_tags_survive_a_consistent_sqlite_copy(archive, tmp_path):
    """The copy is what a backup bundle contains (#161 will wrap this)."""
    from lore.storage.writer import v2_db_path

    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    copied = copy_sqlite_with_sidecars(v2_db_path(archive), copy_dir, 64 * 1024 * 1024)
    assert copied is not None
    (copy_dir / "index_v2.sqlite").write_bytes(Path(copied).read_bytes())
    assert all_tags_by_session(copy_dir) == {"tagged": ["bookmark", "release-check"]}


def test_tag_of_a_deleted_session_keeps_the_annotation(archive):
    """An annotation for an unknown session is not destroyed by the filter path."""
    add_session_tag(archive, "gone-forever", "keep-me")
    assert "keep-me" in all_tags_by_session(archive).get("gone-forever", [])
    entries = [_index_entry(_session("tagged"))]
    listed = filter_sessions(entries, tag="keep-me", user_tags=all_tags_by_session(archive))
    assert listed == []


class _StubExtractor:
    """Minimal extractor: the CLI list path only needs these three members."""

    def __init__(self, sessions):
        self.tool = Tool.CLAUDE_CODE
        self._sessions = sessions

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        return iter(self._sessions)


def _list_args(output_dir: Path, **overrides):
    import argparse

    base = dict(
        tool=None,
        project=None,
        tag=None,
        since=None,
        format="json",
        limit=None,
        offset=0,
        include_automated=False,
        output_dir=str(output_dir),
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_list_filters_by_saved_tag(archive, monkeypatch, capsys):
    """`lore list --tag` reads the same table as the web filter."""
    import lore_cli

    sessions = [_session("tagged"), _session("plain")]
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_StubExtractor(sessions)])
    lore_cli.cmd_list(_list_args(archive, tag="bookmark"))
    payload = json.loads(capsys.readouterr().out)
    assert [entry["session_id"] for entry in payload] == ["tagged"]


def test_cli_list_without_tag_shows_both(archive, monkeypatch, capsys):
    import lore_cli

    sessions = [_session("tagged"), _session("plain")]
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_StubExtractor(sessions)])
    lore_cli.cmd_list(_list_args(archive))
    payload = json.loads(capsys.readouterr().out)
    assert {entry["session_id"] for entry in payload} == {"tagged", "plain"}


def test_cli_search_filters_by_saved_tag(archive, monkeypatch, capsys):
    """`lore search --tag` keeps the ranked hits that carry the tag."""
    import argparse

    import lore_cli

    IndexBuilder(archive).build_index([_session("tagged"), _session("plain")], {})
    args = argparse.Namespace(
        query="xenotoken",
        tool=None,
        project=None,
        tag="bookmark",
        include_automated=False,
        output_dir=str(archive),
    )
    lore_cli.cmd_search(args)
    out = capsys.readouterr().out
    assert "Found 1 result" in out
    assert "tagged" in out


def _mcp_call(monkeypatch, archive: Path, tool: str, arguments: dict) -> dict:
    """Register the real MCP tool group against the fixture archive and call it."""
    import asyncio

    from lore.interfaces import mcp as mcp_module

    index_payload = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(mcp_module, "load_index", lambda: index_payload)
    monkeypatch.setattr(mcp_module, "INDEX_PATH", archive / "index.json")
    server = mcp_module.MCPServer()
    from lore.interfaces.mcp_tools import search as search_tools
    from lore.interfaces.mcp_tools.deps import build_deps

    search_tools.register(server, build_deps(server, mcp_module))
    handler = server.tools[tool]["handler"]
    return json.loads(asyncio.run(handler(arguments)))


def test_mcp_list_sessions_filters_by_saved_tag(archive, monkeypatch):
    IndexBuilder(archive).build_index([_session("tagged"), _session("plain")], {})
    payload = _mcp_call(monkeypatch, archive, "list_sessions", {"tag": "bookmark"})
    assert [entry["id"] for entry in payload["sessions"]] == ["tagged"]


def test_mcp_search_history_filters_by_saved_tag(archive, monkeypatch):
    IndexBuilder(archive).build_index([_session("tagged"), _session("plain")], {})
    payload = _mcp_call(
        monkeypatch, archive, "search_history", {"query": "xenotoken", "tag": "bookmark"}
    )
    assert [entry["id"] for entry in payload["results"]] == ["tagged"]
    assert payload["tag"] == "bookmark"


def test_mcp_search_without_tag_still_returns_both(archive, monkeypatch):
    IndexBuilder(archive).build_index([_session("tagged"), _session("plain")], {})
    payload = _mcp_call(monkeypatch, archive, "search_history", {"query": "xenotoken"})
    assert {entry["id"] for entry in payload["results"]} == {"tagged", "plain"}
