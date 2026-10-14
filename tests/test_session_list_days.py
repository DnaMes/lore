"""Day grouping, date chips, and pagination hand-off for the session list (B5.3)."""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

import pytest

from lore.interfaces import web
from lore.interfaces.web_formatting import (
    format_day_header_label,
    group_sessions_for_list,
    session_day_iso,
)

_FIXED_NOW = datetime(2026, 10, 14, 15, 30, 0)


def _index_with(*sessions: dict) -> dict:
    return {
        "sessions": list(sessions),
        "stats": {
            "total_sessions": len(sessions),
            "total_messages": sum(s.get("messages", 0) for s in sessions),
            "by_tool": {},
            "by_project": {},
        },
        "generated_at": "2026-10-14T00:00:00Z",
    }


def _session(sid: str, updated: str, **extra) -> dict:
    base = {
        "id": sid,
        "tool": "claude-code",
        "title": f"Session {sid}",
        "display_title": f"Session {sid}",
        "created": updated,
        "updated": updated,
        "project": "/repo/lore",
        "messages": 2,
        "prompts": 1,
        "tokens": 100,
    }
    base.update(extra)
    return base


@pytest.fixture()
def list_now(monkeypatch):
    monkeypatch.setattr(web, "sessions_list_now", lambda: _FIXED_NOW)


class _SessionRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_rows = False
        self.children: list[tuple[str, dict]] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "div" and attr.get("id") == "sessionRows":
            self.in_rows = True
            return
        if not self.in_rows:
            return
        if tag == "div" and "day-header" in (attr.get("class") or ""):
            self.children.append(("day-header", attr))
        if tag == "div" and attr.get("role") == "listitem":
            self._current = attr
            self.children.append(("listitem", attr))

    def handle_endtag(self, tag):
        if tag == "div" and self._current is not None:
            self._current = None


@pytest.mark.parametrize(
    "day_iso,expected",
    [
        ("2026-10-14", "Today"),
        ("2026-10-13", "Yesterday"),
        ("2026-10-12", "Monday, Oct 12"),
        ("2026-10-08", "Thursday, Oct 8"),
        ("2026-10-01", "Oct 1"),
        ("2025-12-25", "Dec 25, 2025"),
    ],
)
def test_format_day_header_label(day_iso, expected):
    assert format_day_header_label(day_iso, _FIXED_NOW) == expected


def test_group_counts_per_header():
    sessions = [
        _session("a", "2026-10-14T10:00:00"),
        _session("b", "2026-10-14T09:00:00"),
        _session("c", "2026-10-13T12:00:00"),
    ]
    groups = group_sessions_for_list(sessions, group_mode="day", now=_FIXED_NOW)
    assert groups[0]["label"] == "Today"
    assert groups[0]["count"] == 2
    assert groups[1]["label"] == "Yesterday"
    assert groups[1]["count"] == 1


def test_list_markup_headers_presentation_and_rows_direct_children(list_now, monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(
            _session("t1", "2026-10-14T10:00:00"),
            _session("y1", "2026-10-13T10:00:00"),
        ),
    )
    html = web.app.test_client().get("/sessions").get_data(as_text=True)
    parser = _SessionRowsParser()
    parser.feed(html)
    kinds = [c[0] for c in parser.children]
    assert kinds == ["day-header", "listitem", "day-header", "listitem"]
    assert parser.children[0][1].get("role") == "presentation"
    assert 'class="day-header"' in html
    assert "Today · 1" in html
    assert "Yesterday · 1" in html


def test_rows_after_day_omits_repeated_header(list_now, monkeypatch):
    monkeypatch.setattr(web, "SESSIONS_PER_PAGE", 2)
    sessions = [
        _session("s1", "2026-10-14T12:00:00"),
        _session("s2", "2026-10-14T11:00:00"),
        _session("s3", "2026-10-14T10:00:00"),
        _session("s4", "2026-10-13T09:00:00"),
    ]
    monkeypatch.setattr(web, "load_index", lambda: _index_with(*sessions))
    frag = (
        web.app.test_client()
        .get("/sessions/rows?page=2&after_day=2026-10-14")
        .get_data(as_text=True)
    )
    assert frag.count("day-header") == 1
    assert "Yesterday" in frag
    assert "Today" not in frag


def test_rows_after_day_keeps_header_when_day_changes(list_now, monkeypatch):
    monkeypatch.setattr(web, "SESSIONS_PER_PAGE", 1)
    sessions = [
        _session("s1", "2026-10-14T12:00:00"),
        _session("s2", "2026-10-13T09:00:00"),
    ]
    monkeypatch.setattr(web, "load_index", lambda: _index_with(*sessions))
    frag = (
        web.app.test_client()
        .get("/sessions/rows?page=2&after_day=2026-10-14")
        .get_data(as_text=True)
    )
    assert "Yesterday" in frag
    assert frag.count("day-header") == 1


def test_data_last_day_on_container_and_partial(list_now, monkeypatch):
    monkeypatch.setattr(web, "SESSIONS_PER_PAGE", 2)
    sessions = [
        _session("s1", "2026-10-14T12:00:00"),
        _session("s2", "2026-10-14T11:00:00"),
        _session("s3", "2026-10-13T10:00:00"),
    ]
    monkeypatch.setattr(web, "load_index", lambda: _index_with(*sessions))
    client = web.app.test_client()
    page = client.get("/sessions").get_data(as_text=True)
    assert 'data-last-day="2026-10-14"' in page
    frag = client.get("/sessions/rows?page=2").get_data(as_text=True)
    assert 'data-last-day="2026-10-13"' in frag


def test_group_flat_no_headers(list_now, monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(
            _session("a", "2026-10-14T10:00:00"),
            _session("b", "2026-10-13T10:00:00"),
        ),
    )
    html = web.app.test_client().get("/sessions?group=flat").get_data(as_text=True)
    rows_html = html.split('id="sessionRows"', 1)[1].split("</section>", 1)[0]
    assert 'class="day-header"' not in rows_html
    assert 'data-group="flat"' in html


def test_load_more_script_preserves_group(list_now, monkeypatch):
    monkeypatch.setattr(
        web, "load_index", lambda: _index_with(_session("a", "2026-10-14T10:00:00"))
    )
    html = web.app.test_client().get("/sessions?group=flat").get_data(as_text=True)
    assert "group" in html
    assert re.search(r"params\.set\(['\"]group['\"]", html) or "group:" in html


def test_date_chip_links_and_active_aria(list_now, monkeypatch):
    monkeypatch.setattr(
        web, "load_index", lambda: _index_with(_session("a", "2026-10-14T10:00:00"))
    )
    html = (
        web.app.test_client()
        .get("/sessions?start=2026-10-14&end=2026-10-14")
        .get_data(as_text=True)
    )
    assert 'href="/sessions?start=2026-10-14&amp;end=2026-10-14' in html or (
        "start=2026-10-14" in html and "end=2026-10-14" in html
    )
    assert 'aria-current="true"' in html
    assert "date-chip" in html


def test_session_day_iso_uses_updated():
    assert session_day_iso(_session("x", "2026-10-14T08:00:00", created="2026-01-01T00:00:00")) == (
        "2026-10-14"
    )


def test_sessions_without_dates_get_a_readable_group_label():
    from lore.interfaces.web_formatting import group_sessions_for_list

    groups = group_sessions_for_list([{"id": "a"}, {"id": "b", "created": "garbage"}])

    assert [g["label"] for g in groups] == ["Unknown date"]
