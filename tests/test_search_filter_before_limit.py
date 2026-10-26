"""Filters constrain the candidate set before ranking limits it (#174).

The historical behaviour fetched the best ``N * 4`` unfiltered hits and dropped
non-matching rows in Python, so a query whose best matches lived in another
tool, project, date range or tag returned fewer hits than requested — or none.
These tests build the "top hits belong elsewhere" fixture and then check the
public entry points with ``limit=1``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services.index import search_index
from lore.storage import add_session_tag, search_sessions, v2_db_path, write_sessions
from lore.storage.embeddings import embeddings_available, sqlite_vec_available
from lore.storage.search import session_filter_sql

requires_vectors = pytest.mark.skipif(
    not (sqlite_vec_available() and embeddings_available()),
    reason="needs the 'semantic' extra (sqlite-vec + fastembed)",
)

PHRASE = "zebraphrase"


def _session(
    session_id: str,
    *,
    tool: Tool = Tool.CLAUDE_CODE,
    project: str = "/repo/elsewhere",
    created: datetime = datetime(2026, 1, 1),
) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=created,
        last_updated=created,
        project_path=project,
        title=f"Session {session_id}",
        messages=[
            UnifiedMessage(Role.USER, f"please mention {PHRASE} here", created),
            UnifiedMessage(Role.ASSISTANT, "acknowledged", created),
        ],
    )


@pytest.fixture()
def elsewhere_first(tmp_path):
    """Three sessions; the two best-ranked ones fail every filter below."""
    sessions = [
        # Ranked first: wrong tool, wrong project, wrong month.
        _session("noise-1", tool=Tool.CURSOR, created=datetime(2026, 5, 1)),
        _session("noise-2", tool=Tool.CURSOR, created=datetime(2026, 5, 2)),
        # The only session that survives the filters.
        _session(
            "target",
            tool=Tool.CLAUDE_CODE,
            project="/repo/lore",
            created=datetime(2026, 6, 15),
        ),
    ]
    write_sessions(v2_db_path(tmp_path), sessions)
    add_session_tag(tmp_path, "noise-1", "other")
    add_session_tag(tmp_path, "target", "keep")
    return tmp_path


# ---------------------------------------------------------------------------
# the SQL filter builder itself
# ---------------------------------------------------------------------------


def test_filter_sql_is_empty_without_filters():
    assert session_filter_sql() == ("", ())


def test_filter_sql_tool_project_tag_and_dates():
    sql, params = session_filter_sql(
        tool="claude-code", project="/repo/a_b%c", tag="Keep", start="2026-06-01", end="2026-06-30"
    )
    assert "s.tool = ?" in sql
    assert "s.project LIKE ? ESCAPE" in sql
    assert "EXISTS (SELECT 1 FROM session_tags" in sql
    assert sql.count("substr(s.created, 1, 10)") == 2
    # % and _ in the project filter are escaped, the date bounds are day prefixes
    assert params == ("claude-code", "%/repo/a\\_b\\%c%", "keep", "2026-06-01", "2026-06-30")


@pytest.mark.parametrize("field", ["start", "end"])
def test_invalid_date_raises(field):
    with pytest.raises(ValueError, match=f"Invalid {field} date"):
        session_filter_sql(**{field: "not-a-date"})


def test_filter_sql_executes_against_a_real_table():
    """The generated clause is valid SQL and selects only matching rows."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT, tool TEXT, project TEXT, created TEXT);
        CREATE TABLE session_tags (session_id TEXT, tag TEXT);
        INSERT INTO sessions VALUES ('a', 'claude-code', '/repo/lore', '2026-06-15T10:00:00');
        INSERT INTO sessions VALUES ('b', 'cursor', '/repo/lore', '2026-06-16T10:00:00');
        INSERT INTO sessions VALUES ('c', 'claude-code', '/repo/other', '2026-07-01T10:00:00');
        INSERT INTO session_tags VALUES ('a', 'keep');
        """
    )
    sql, params = session_filter_sql(
        tool="claude-code", project="/repo/lore", tag="keep", start="2026-06-01", end="2026-06-30"
    )
    rows = conn.execute(f"SELECT id FROM sessions s WHERE 1=1{sql} LIMIT 1", params).fetchall()
    assert rows == [("a",)]
    conn.close()


# ---------------------------------------------------------------------------
# v2 FTS path
# ---------------------------------------------------------------------------


def test_fts_filter_applies_before_the_limit(elsewhere_first):
    """limit=1 must return the matching session, not an empty result."""
    hits = search_sessions(elsewhere_first, PHRASE, tool="claude-code", limit=1)
    assert [hit["session"]["id"] for hit in hits] == ["target"]


def test_fts_project_filter_applies_before_the_limit(elsewhere_first):
    hits = search_sessions(elsewhere_first, PHRASE, project="/repo/lore", limit=1)
    assert [hit["session"]["id"] for hit in hits] == ["target"]


def test_fts_date_filter_applies_before_the_limit(elsewhere_first):
    hits = search_sessions(elsewhere_first, PHRASE, start="2026-06-01", end="2026-06-30", limit=1)
    assert [hit["session"]["id"] for hit in hits] == ["target"]


def test_fts_tag_filter_applies_before_the_limit(elsewhere_first):
    hits = search_sessions(elsewhere_first, PHRASE, tag="keep", limit=1)
    assert [hit["session"]["id"] for hit in hits] == ["target"]


def test_fts_unfiltered_limit_still_returns_the_best_hit(elsewhere_first):
    hits = search_sessions(elsewhere_first, PHRASE, limit=1)
    assert len(hits) == 1


def test_fts_invalid_date_raises(elsewhere_first):
    with pytest.raises(ValueError, match="Invalid start date"):
        search_sessions(elsewhere_first, PHRASE, start="15/06/2026")


def test_fts_filter_that_matches_nothing_is_empty(elsewhere_first):
    assert search_sessions(elsewhere_first, PHRASE, tag="does-not-exist", limit=1) == []


# ---------------------------------------------------------------------------
# search_index wrapper (the path every surface uses)
# ---------------------------------------------------------------------------


def test_search_index_passes_filters_through(elsewhere_first):
    index_path = elsewhere_first / "index.json"
    index_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
    results = search_index(index_path, PHRASE, set(), tag="keep", limit=1)
    assert [result["session"]["id"] for result in results] == ["target"]


def test_search_index_rejects_an_invalid_date(elsewhere_first):
    index_path = elsewhere_first / "index.json"
    index_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid end date"):
        search_index(index_path, PHRASE, set(), end="2026-13-45")


# ---------------------------------------------------------------------------
# semantic path — needs the optional vector backend
# ---------------------------------------------------------------------------


@requires_vectors
def test_semantic_filter_applies_before_the_knn_cutoff(tmp_path):
    """The nearest neighbours fail the filter; limit=1 must still find the match."""
    from lore.storage import semantic_search_sessions

    sessions = [
        _session(f"near-{i}", tool=Tool.CURSOR, project="/repo/elsewhere") for i in range(3)
    ]
    sessions.append(_session("target", project="/repo/lore"))
    write_sessions(v2_db_path(tmp_path), sessions)

    hits = semantic_search_sessions(
        tmp_path, f"please mention {PHRASE} here", project="/repo/lore", limit=1
    )
    assert [hit["session"]["id"] for hit in hits] == ["target"]


# ---------------------------------------------------------------------------
# HTTP surfaces
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_client(elsewhere_first, monkeypatch):
    """Session-level monkeypatches for the archive the search wrappers read.

    ``web.search_index`` is imported from ``web_data``, which captured
    ``INDEX_PATH``/``OUTPUT_DIR`` at import time, so both modules are patched —
    the real ``~/.lore`` archive must never be touched by these tests.
    """
    from lore.interfaces import web, web_data

    index_path = elsewhere_first / "index.json"
    index_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", elsewhere_first)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    with web.app.test_client() as client:
        yield client


def test_api_v1_search_applies_the_tag_filter(web_client):
    response = web_client.get(f"/api/v1/search?q={PHRASE}&tag=keep&limit=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert [result["id"] for result in payload["results"]] == ["target"]
    assert payload["tag"] == "keep"


def test_api_v1_search_reports_an_invalid_date(web_client):
    response = web_client.get(f"/api/v1/search?q={PHRASE}&end=2026-13-45")
    assert response.status_code == 400
    assert "Invalid end date" in response.get_json()["error"]


def test_api_search_applies_the_date_filter(web_client):
    response = web_client.get(f"/api/search?q={PHRASE}&start=2026-06-01&end=2026-06-30")
    assert response.status_code == 200
    assert [result["id"] for result in response.get_json()] == ["target"]


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_mcp_search_history_applies_tag_and_cap(elsewhere_first, monkeypatch):
    import asyncio

    from lore.interfaces import mcp as mcp_module
    from lore.interfaces.mcp_tools import search as search_tools
    from lore.interfaces.mcp_tools.deps import build_deps

    index_path = elsewhere_first / "index.json"
    index_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
    monkeypatch.setattr(mcp_module, "INDEX_PATH", index_path)
    server = mcp_module.MCPServer()
    search_tools.register(server, build_deps(server, mcp_module))
    payload = json.loads(
        asyncio.run(
            server.tools["search_history"]["handler"]({"query": PHRASE, "tag": "keep", "limit": 1})
        )
    )
    assert [result["id"] for result in payload["results"]] == ["target"]


def test_mcp_list_sessions_rejects_an_invalid_start(elsewhere_first, monkeypatch):
    import asyncio

    from lore.interfaces import mcp as mcp_module
    from lore.interfaces.mcp_tools import search as search_tools
    from lore.interfaces.mcp_tools.deps import build_deps

    index_path = elsewhere_first / "index.json"
    index_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
    monkeypatch.setattr(mcp_module, "INDEX_PATH", index_path)
    server = mcp_module.MCPServer()
    search_tools.register(server, build_deps(server, mcp_module))
    message = asyncio.run(server.tools["list_sessions"]["handler"]({"start": "not-a-date"}))
    assert message == "Invalid start parameter."
