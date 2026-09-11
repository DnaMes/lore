"""Direct single-session lookup fast path (#127).

Cold-cache bulk collection (``load_sessions_for_tool`` → ``collect_sessions``)
parses every session file of a tool just to serve one ``load_session_by_id`` /
``load_live_session`` call. Extractors with deterministic layouts now resolve
the session by parsing only its source file. These tests pin the
``BaseExtractor.find_session_by_id`` contract, the claude override, the
``find_live_session`` service helper, and the fast-path wiring in both the web
and MCP lookup entry points.
"""

from __future__ import annotations

import json
import types
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor
from lore.extractors.claude import ClaudeCodeExtractor
from lore.interfaces import web
from lore.interfaces.mcp_tools.deps import build_deps
from lore.interfaces.server import MCPServer
from lore.services import extraction as services_extraction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(session_id: str, tool: Tool = Tool.CLAUDE_CODE) -> UnifiedSession:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=[UnifiedMessage(role=Role.USER, content="hi", timestamp=now)],
        project_path="/tmp/proj",
        thread_id="thread-1",
        title="Title",
    )


def _claude_jsonl(*user_prompts: str) -> str:
    """Minimal valid Claude Code JSONL with one assistant reply per prompt."""
    lines = []
    for prompt in user_prompts:
        lines.append(json.dumps({"type": "user", "message": {"content": prompt}}))
        lines.append(json.dumps({"type": "assistant", "message": {"content": "ok"}}))
    return "\n".join(lines) + "\n"


class _FakeExtractor(BaseExtractor):
    """Extractor whose direct lookup is scriptable per test."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def find_session_by_id(self, session_id):
        self.calls.append(session_id)
        return self.result

    def extract_sessions(self):
        return iter([])
        yield  # pragma: no cover - keeps this a generator


# ---------------------------------------------------------------------------
# BaseExtractor contract
# ---------------------------------------------------------------------------


def test_base_extractor_default_find_session_by_id_returns_none():
    extractor = _FakeExtractor()
    assert extractor.find_session_by_id("ses-anywhere") is None


# ---------------------------------------------------------------------------
# Claude override
# ---------------------------------------------------------------------------


def _claude_extractor_with(tmp_projects: Path) -> ClaudeCodeExtractor:
    extractor = ClaudeCodeExtractor()
    extractor.base_paths = [tmp_projects]
    return extractor


def test_claude_find_session_by_id_direct_hit(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / "-home-user-proj"
    project_dir.mkdir(parents=True)
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (project_dir / f"{session_id}.jsonl").write_text(
        _claude_jsonl("first prompt", "second prompt", "third prompt"), encoding="utf-8"
    )
    # A sibling transcript that must NOT be read/parsed for this lookup.
    (project_dir / "ffffffff-0000-0000-0000-000000000000.jsonl").write_text(
        _claude_jsonl("other session"), encoding="utf-8"
    )

    session = _claude_extractor_with(projects).find_session_by_id(session_id)

    assert session is not None
    assert session.session_id == session_id
    assert session.source_path == str(project_dir / f"{session_id}.jsonl")
    assert len(session.messages) == 6


def test_claude_find_session_by_id_miss_returns_none(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-home-user-proj").mkdir(parents=True)
    extractor = _claude_extractor_with(projects)
    assert extractor.find_session_by_id("no-such-session") is None


def test_claude_find_session_by_id_unavailable(tmp_path):
    extractor = ClaudeCodeExtractor()
    extractor.base_paths = []
    assert extractor.find_session_by_id("ses-anything") is None


def test_claude_find_session_by_id_skips_low_quality_session(tmp_path, monkeypatch):
    """A session the quality filter would drop is not returned (parity with
    the bulk path, which never yields it either). conftest relaxes
    MIN_USER_PROMPTS to 1 suite-wide, so restore the production default
    for this assertion."""
    monkeypatch.setattr(ClaudeCodeExtractor, "MIN_USER_PROMPTS", 3, raising=True)
    projects = tmp_path / "projects"
    project_dir = projects / "-home-user-proj"
    project_dir.mkdir(parents=True)
    (project_dir / "short-session.jsonl").write_text(
        _claude_jsonl("only one prompt"), encoding="utf-8"
    )
    extractor = _claude_extractor_with(projects)
    assert extractor.find_session_by_id("short-session") is None


# ---------------------------------------------------------------------------
# find_live_session service helper
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_extractor_registry(monkeypatch):
    """Route find_live_session through a scriptable fake extractor."""

    def _install(result, expected_session_id=None):
        fake = _FakeExtractor(result)

        def _select(tool_filter=None, extractors=None):
            if tool_filter and tool_filter != fake.tool.value:
                return []
            return [fake]

        monkeypatch.setattr(services_extraction, "select_extractors", _select)
        return fake

    return _install


def test_find_live_session_returns_matching_session(fake_extractor_registry):
    session = _make_session("ses-fast")
    fake_extractor_registry(session)

    found = services_extraction.find_live_session("ses-fast", "claude-code")
    assert found is session


def test_find_live_session_rejects_id_mismatch(fake_extractor_registry):
    session = _make_session("ses-other")
    fake_extractor_registry(session)

    assert services_extraction.find_live_session("ses-requested") is None


def test_find_live_session_respects_deleted_ids(fake_extractor_registry):
    session = _make_session("ses-tombstoned")
    fake = fake_extractor_registry(session)

    assert (
        services_extraction.find_live_session(
            "ses-tombstoned", "claude-code", deleted_ids={"ses-tombstoned"}
        )
        is None
    )
    assert fake.calls == []  # tombstone short-circuits before any extractor runs


def test_find_live_session_skips_broken_extractor(monkeypatch):
    """One broken tool never aborts the lookup (mirrors collect_sessions)."""

    class _BrokenExtractor(_FakeExtractor):
        def find_session_by_id(self, session_id):
            raise RuntimeError("boom")

    broken = _BrokenExtractor()
    healthy = _FakeExtractor(_make_session("ses-ok"))

    def _select(tool_filter=None, extractors=None):
        return [broken, healthy]

    monkeypatch.setattr(services_extraction, "select_extractors", _select)

    found = services_extraction.find_live_session("ses-ok")
    assert found is not None
    assert found.session_id == "ses-ok"


# ---------------------------------------------------------------------------
# Web entry point wiring
# ---------------------------------------------------------------------------


def test_web_load_session_by_id_uses_fast_path(monkeypatch):
    session = _make_session("ses-web-fast")

    def _boom(tool=None):
        raise AssertionError("bulk loader must not run when the fast path hits")

    monkeypatch.setattr(web, "find_live_session", lambda sid, tool=None: session)
    monkeypatch.setattr(web, "load_sessions_for_tool", _boom)

    assert (
        web.load_session_by_id(
            "ses-web-fast", preferred_tool="claude-code", allow_cross_tool_fallback=False
        )
        is session
    )


def test_web_load_session_by_id_falls_back_to_bulk(monkeypatch):
    session = _make_session("ses-web-bulk")
    monkeypatch.setattr(web, "find_live_session", lambda sid, tool=None: None)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda tool=None: [session])

    assert (
        web.load_session_by_id(
            "ses-web-bulk", preferred_tool="claude-code", allow_cross_tool_fallback=False
        )
        is session
    )


# ---------------------------------------------------------------------------
# MCP entry point wiring
# ---------------------------------------------------------------------------


def _fake_mcp_module(tmp_path, fast_result, bulk_result):
    """Module stand-in for build_deps with scriptable lookup functions."""

    def _bulk(tool=None):
        if isinstance(bulk_result, Exception):
            raise bulk_result
        return bulk_result

    return types.SimpleNamespace(
        load_index=lambda: {"sessions": [], "stats": {}},
        load_sessions_for_tool=_bulk,
        search_index=lambda *a, **k: [],
        find_live_session_by_id=lambda sid, tool=None: fast_result,
        INDEX_PATH=tmp_path / "index.json",
    )


def test_mcp_load_live_session_uses_fast_path(tmp_path, monkeypatch):
    session = _make_session("ses-mcp-fast")
    # Pre-write the index so ensure_index() short-circuits instead of
    # running a real extraction against the host HOME.
    (tmp_path / "index.json").write_text(
        json.dumps({"sessions": [], "stats": {}}), encoding="utf-8"
    )
    fake = _fake_mcp_module(
        tmp_path, session, AssertionError("bulk loader must not run on fast path")
    )
    deps = build_deps(MCPServer(), fake)

    assert deps.load_live_session("ses-mcp-fast", tool_filter="claude-code") is session


def test_mcp_load_live_session_falls_back_to_bulk(tmp_path):
    session = _make_session("ses-mcp-bulk")
    (tmp_path / "index.json").write_text(
        json.dumps({"sessions": [], "stats": {}}), encoding="utf-8"
    )
    fake = _fake_mcp_module(tmp_path, None, [session])
    deps = build_deps(MCPServer(), fake)

    assert deps.load_live_session("ses-mcp-bulk", tool_filter="claude-code") is session
