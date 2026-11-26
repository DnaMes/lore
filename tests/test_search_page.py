"""Tests for the server-rendered /search page and palette search wiring."""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.storage import v2_db_path, write_sessions


def _make_session(
    sid: str,
    *,
    tool: Tool = Tool.CLAUDE_CODE,
    project: str = "/repo/alpha",
    title: str = "Session title",
    body: str = "hello world",
    created: datetime = datetime(2026, 4, 10, 10, 0, 0),
) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=created,
        last_updated=created,
        project_path=project,
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=created)],
    )


@pytest.fixture()
def search_client(monkeypatch, tmp_path):
    """Test client backed by a small v2 store in tmp_path."""
    output_dir = tmp_path / ".lore"
    output_dir.mkdir()
    index_path = output_dir / "index.json"
    deleted_path = output_dir / "deleted_sessions.json"

    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", output_dir)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
        monkeypatch.setattr(module, "DELETED_SESSIONS_PATH", deleted_path)

    def _seed(sessions: list[UnifiedSession]) -> None:
        write_sessions(v2_db_path(output_dir), sessions)
        web.clear_index_cache()

    with web.app.test_client() as client:
        yield client, _seed


def test_search_empty_query_shows_form_only(search_client):
    client, seed = search_client
    seed([_make_session("s1", body="zebra crossing")])
    resp = client.get("/search")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="q"' in html
    assert "No results" not in html
    assert "result" not in html.lower() or "Search" in html


def test_search_zebra_renders_title_and_mark(search_client):
    client, seed = search_client
    seed([_make_session("s1", title="Zebra debugging", body="found a zebra in logs")])
    resp = client.get("/search?q=zebra")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Zebra debugging" in html
    assert "<mark>" in html


def test_search_filters_narrow_results(search_client):
    client, seed = search_client
    seed(
        [
            _make_session(
                "match",
                tool=Tool.CODEX,
                project="/repo/target",
                title="Target zebra",
                body="zebra",
                created=datetime(2026, 4, 15, 12, 0, 0),
            ),
            _make_session(
                "wrong-tool",
                tool=Tool.CLAUDE_CODE,
                project="/repo/target",
                title="Claude zebra",
                body="zebra",
                created=datetime(2026, 4, 15, 12, 0, 0),
            ),
            _make_session(
                "wrong-project",
                tool=Tool.CODEX,
                project="/repo/other",
                title="Other project zebra",
                body="zebra",
                created=datetime(2026, 4, 15, 12, 0, 0),
            ),
            _make_session(
                "wrong-date",
                tool=Tool.CODEX,
                project="/repo/target",
                title="Old zebra",
                body="zebra",
                created=datetime(2026, 1, 5, 12, 0, 0),
            ),
        ]
    )
    resp = client.get(
        "/search?q=zebra&tool=codex&project=/repo/target&start=2026-04-01&end=2026-04-30"
    )
    assert resp.status_code == 200
    # The sidebar "Recent activity" lists every session; only the results column is filtered.
    html = resp.get_data(as_text=True).split('id="main-content"', 1)[1]
    assert "Target zebra" in html
    assert "Claude zebra" not in html
    assert "Other project zebra" not in html
    assert "Old zebra" not in html


def test_search_pagination_next_and_page_two(search_client):
    client, seed = search_client
    sessions = [
        _make_session(
            f"s-{i:02d}",
            title=f"Zebra session {i:02d}",
            body="zebra",
            created=datetime(2026, 4, 1, 10, i, 0),
        )
        for i in range(25)
    ]
    seed(sessions)

    page1 = client.get("/search?q=zebra")
    assert page1.status_code == 200
    html1 = page1.get_data(as_text=True)
    assert "25 results" in html1
    assert "Next" in html1
    assert "page=2" in html1

    page2 = client.get("/search?q=zebra&page=2")
    assert page2.status_code == 200
    html2 = page2.get_data(as_text=True)
    assert "Previous" in html2
    assert html2.count("<article ") == 5


def test_search_invalid_date_returns_400(search_client):
    client, seed = search_client
    seed([_make_session("s1", body="zebra")])
    resp = client.get("/search?q=zebra&start=not-a-date")
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "Invalid start date" in html


def test_search_script_tag_in_query_is_escaped(search_client):
    client, seed = search_client
    seed([])
    resp = client.get("/search?q=<script>alert(1)</script>")
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_api_search_includes_tool_name(search_client):
    client, seed = search_client
    seed([_make_session("s1", tool=Tool.CLAUDE_CODE, title="Zebra API", body="zebra")])
    resp = client.get("/api/search?q=zebra")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert len(payload) == 1
    assert payload[0]["tool"] == "claude-code"
    assert payload[0]["tool_name"] == "Claude"


def test_base_palette_js_references_search_route_and_aria_selected(search_client):
    client, seed = search_client
    seed([])
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/search?q=" in html
    assert "aria-selected" in html
