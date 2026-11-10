"""Tests for the dashboard library-home sections (B5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from lore.interfaces import web
from lore.interfaces.library_home import (
    CONTINUE_LIMIT,
    PROJECTS_LIMIT,
    RECENT_DAYS_LIMIT,
    build_library_home_context,
    format_relative_activity_time,
    project_page_url,
    short_project_label,
)
from lore.storage.summaries import save_summary


def _session(
    sid: str,
    *,
    updated: str,
    created: str | None = None,
    project: str = "/home/user/projects/ai-stack/lore",
    origin: str = "",
    title: str = "Title",
    display_title: str | None = None,
    tool: str = "claude-code",
) -> dict:
    return {
        "id": sid,
        "tool": tool,
        "title": title,
        "display_title": display_title if display_title is not None else title,
        "created": created or updated,
        "updated": updated,
        "project": project,
        "origin": origin,
        "messages": 1,
        "prompts": 1,
    }


def _index(sessions: list[dict]) -> dict:
    return {
        "sessions": sessions,
        "stats": {
            "total_sessions": len(sessions),
            "total_messages": len(sessions),
            "by_tool": {},
            "by_project": {},
        },
    }


def test_continue_limits_six_newest_interactive_first(tmp_path: Path) -> None:
    sessions = [
        _session(f"s{i}", updated=f"2026-11-{i:02d}T12:00:00", title=f"T{i}") for i in range(1, 10)
    ]
    sessions.append(_session("auto", updated="2026-11-11T12:00:00", origin="exec", title="Auto"))

    ctx = build_library_home_context(_index(sessions), tmp_path)

    ids = [c["id"] for c in ctx["library_continue"]]
    assert len(ids) == CONTINUE_LIMIT
    assert ids[0] == "s9"
    assert "auto" not in ids


def test_projects_shelf_top_twelve_by_last_activity(tmp_path: Path) -> None:
    sessions = []
    for i in range(15):
        sessions.append(
            _session(
                f"p{i}",
                updated=f"2026-10-{15 - i:02d}T10:00:00",
                project=f"/repos/project-{i}",
                title=f"Latest {i}",
            )
        )

    ctx = build_library_home_context(_index(sessions), tmp_path)
    shelf = ctx["library_projects"]

    assert len(shelf) == PROJECTS_LIMIT
    assert shelf[0]["path"] == "/repos/project-0"
    assert shelf[-1]["path"] == "/repos/project-11"


def test_recent_days_seven_most_recent_active_days(tmp_path: Path) -> None:
    sessions = []
    for day in range(1, 12):
        sessions.append(
            _session(f"d{day}a", updated=f"2026-11-{day:02d}T08:00:00", title=f"Day {day} a")
        )
        sessions.append(
            _session(f"d{day}b", updated=f"2026-11-{day:02d}T09:00:00", title=f"Day {day} b")
        )
        if day == 11:
            sessions.append(
                _session(f"d{day}c", updated=f"2026-11-{day:02d}T10:00:00", title=f"Day {day} c")
            )

    ctx = build_library_home_context(_index(sessions), tmp_path)
    days = ctx["library_recent_days"]

    assert len(days) == RECENT_DAYS_LIMIT
    assert days[0]["date"] == "2026-11-11"
    assert days[-1]["date"] == "2026-11-05"
    assert days[0]["session_count"] == 3
    assert len(days[0]["sessions"]) == 3


def test_summary_attached_when_store_has_one_liner(tmp_path: Path) -> None:
    sid = "summarized"
    sessions = [_session(sid, updated="2026-11-10T10:00:00")]
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{sid}",
        session_id=sid,
        fingerprint="fp",
        model="test",
        prompt_version=1,
        compressor_version=1,
        payload={"one_liner": "Fixed the redirect bug"},
    )

    ctx = build_library_home_context(_index(sessions), tmp_path)

    assert ctx["library_continue"][0]["summary"] == "Fixed the redirect bug"


def test_summary_absent_when_missing(tmp_path: Path) -> None:
    sessions = [_session("plain", updated="2026-11-10T10:00:00")]
    ctx = build_library_home_context(_index(sessions), tmp_path)
    assert ctx["library_continue"][0].get("summary") is None


def test_list_summaries_single_batch_call(monkeypatch, tmp_path: Path) -> None:
    calls: list[int] = []

    def spy(output_dir, level=None, session_ids=None):
        ids = list(session_ids or [])
        calls.append(len(ids))
        from lore.storage.summaries import list_summaries as real

        return real(output_dir, level=level, session_ids=ids)

    monkeypatch.setattr("lore.interfaces.library_home.list_summaries", spy)
    sessions = [
        _session(f"s{i}", updated=f"2026-11-{i:02d}T10:00:00", project=f"/p/{i}")
        for i in range(1, 8)
    ]
    build_library_home_context(_index(sessions), tmp_path)
    assert len(calls) == 1


def test_short_project_label_uses_last_two_segments() -> None:
    assert short_project_label("/home/user/projects/games/kickzo") == "games/kickzo"
    assert short_project_label("/repo/demo") == "repo/demo"


def test_project_page_url_under_projects() -> None:
    path = "/home/user/projects/ai-stack/lore"
    assert project_page_url(path) == f"/projects?project={quote(path, safe='')}"


def test_format_relative_activity_time_hours_and_date() -> None:
    now = datetime(2026, 11, 10, 14, 0, 0)
    assert format_relative_activity_time("2026-11-10T12:00:00", now=now) == "2 h ago"
    assert format_relative_activity_time("2026-11-01T12:00:00", now=now) == "2026-11-01"


def test_empty_index_renders_without_error(monkeypatch, tmp_path: Path) -> None:
    fake_output = tmp_path / ".lore"
    fake_output.mkdir()
    fake_index = fake_output / "index.json"
    fake_index.write_text("{}", encoding="utf-8")
    stats = {
        "total_sessions": 0,
        "total_messages": 0,
        "by_tool": {},
        "by_project": {},
    }
    monkeypatch.setattr(web, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web, "INDEX_PATH", fake_index)
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": [], "stats": stats})

    with web.app.test_client() as client:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
    assert "Continue where you left off" in body


def test_dashboard_escapes_html_in_titles(monkeypatch, tmp_path: Path) -> None:
    evil = "<img src=x onerror=alert(1)>"
    sessions = [_session("xss", updated="2026-11-10T10:00:00", title=evil, display_title=evil)]
    stats = {
        "total_sessions": 1,
        "total_messages": 1,
        "by_tool": {"claude-code": 1},
        "by_project": {},
    }
    fake_output = tmp_path / ".lore"
    fake_output.mkdir()
    fake_index = fake_output / "index.json"
    fake_index.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web, "INDEX_PATH", fake_index)
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": sessions, "stats": stats})

    with web.app.test_client() as client:
        html = client.get("/").get_data(as_text=True)

    assert evil not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_projects_page_filters_to_one_project_and_lists_all_its_sessions(monkeypatch):
    """The dashboard's project links (/projects?project=<path>) open that project alone,
    with every session instead of the overview's three."""
    from lore.interfaces import web

    sessions = [
        {
            "id": f"a{i}",
            "title": f"alpha {i}",
            "tool": "claude-code",
            "project": "/w/alpha",
            "created": f"2026-11-0{i}T10:00:00",
            "updated": f"2026-11-0{i}T10:00:00",
        }
        for i in range(1, 6)
    ] + [
        {
            "id": "b1",
            "title": "beta one",
            "tool": "codex",
            "project": "/w/beta",
            "created": "2026-11-01T10:00:00",
            "updated": "2026-11-01T10:00:00",
        }
    ]
    payload = {"sessions": sessions, "stats": {}}
    monkeypatch.setattr(web, "load_index", lambda: payload)
    monkeypatch.setattr(web, "_visible_index", lambda: payload)
    with web.app.test_client() as client:
        page = client.get("/projects?project=/w/alpha").get_data(as_text=True)
        assert all(f"alpha {i}" in page for i in range(1, 6))
        # beta may still appear in the sidebar's recent activity, never as a project card row.
        assert 'href="/session/b1" class="list-row' not in page
        assert all(f'href="/session/a{i}" class="list-row' in page for i in range(1, 6))
        assert client.get("/projects?project=%3Cx%3E").status_code in (200, 400)
