"""Markup contract tests for compact session list rows (issue #197, B5.1)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from lore.interfaces import web
from lore.interfaces.web_formatting import should_show_prompt_outline

_ISO_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _index_with(*sessions: dict) -> dict:
    return {
        "sessions": list(sessions),
        "stats": {
            "total_sessions": len(sessions),
            "total_messages": sum(s.get("messages", 0) for s in sessions),
            "by_tool": {},
            "by_project": {},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }


def _full_session(**overrides) -> dict:
    base = {
        "id": "sess-markup-1",
        "tool": "claude-code",
        "title": "Fix the session list layout",
        "display_title": "Fix the session list layout",
        "created": "2026-04-01T10:00:00",
        "updated": "2026-04-01T11:05:00",
        "project": "/repo/lore",
        "messages": 10,
        "prompts": 3,
        "tokens": 12400,
        "prompt_outline": "Different outline text for the first user prompt here.",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with(_full_session()))
    return web.app.test_client()


class _ListRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_session_rows = False
        self.listitems: list[dict] = []
        self._current: dict | None = None
        self._in_link = False
        self._in_menu = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "div" and attr.get("id") == "sessionRows":
            self.in_session_rows = True
            return
        if not self.in_session_rows:
            return
        if tag == "div" and attr.get("role") == "listitem":
            self._current = {"links": [], "menu": False, "form_in_link": False}
            self.listitems.append(self._current)
            return
        if self._current is None:
            return
        if tag == "a" and not self._in_menu:
            self._in_link = True
            self._current["links"].append(attr.get("href", ""))
        if tag == "details" and "row-menu" in (attr.get("class") or ""):
            self._in_menu = True
            self._current["menu"] = True
        if tag == "form" and self._in_link:
            self._current["form_in_link"] = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False
        if tag == "details":
            self._in_menu = False


def test_sessions_list_roles_and_row_links(client):
    html = client.get("/sessions").get_data(as_text=True)
    assert 'id="sessionRows"' in html and 'role="list"' in html
    parser = _ListRowParser()
    parser.feed(html)
    assert len(parser.listitems) == 1
    row = parser.listitems[0]
    assert len(row["links"]) == 1
    assert row["links"][0].startswith("/session/sess-markup-1")
    assert row["menu"] is True
    assert row["form_in_link"] is False
    assert 'action="/session/sess-markup-1/delete"' in html


def test_no_always_visible_red_delete_button(client):
    html = client.get("/sessions").get_data(as_text=True)
    rows_html = html.split('id="sessionRows"', 1)[1].split("</section>", 1)[0]
    assert "text-red-600" not in rows_html
    assert "border-red-200" not in rows_html


def test_meta_line_shows_prompts_tokens_duration(client):
    html = client.get("/sessions").get_data(as_text=True)
    assert "3 prompts" in html
    assert "12.4k" in html
    assert "1h 5m" in html


def test_meta_omits_unknown_tokens_and_short_duration(monkeypatch):
    entry = _full_session(updated="2026-04-01T10:00:30")
    entry.pop("tokens", None)
    monkeypatch.setattr(web, "load_index", lambda: _index_with(entry))
    html = web.app.test_client().get("/sessions").get_data(as_text=True)
    assert "12.4k" not in html
    assert "prompts" in html
    assert "1h" not in html
    assert "30m" not in html


def test_time_element_has_valid_datetime(client):
    html = client.get("/sessions").get_data(as_text=True)
    for match in re.finditer(r'<time[^>]+datetime="([^"]+)"', html):
        assert _ISO_TIME_RE.match(match.group(1))


def test_outline_hidden_when_duplicate_title(monkeypatch):
    title = "Fix the session list layout please review"
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(
            _full_session(
                title=title,
                display_title=title,
                prompt_outline=title + " extra",
            )
        ),
    )
    html = web.app.test_client().get("/sessions").get_data(as_text=True)
    assert 'class="session-row-outline"' not in html


def test_outline_shown_when_different(monkeypatch):
    outline = "Completely separate outline about database migrations."
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(_full_session(prompt_outline=outline)),
    )
    html = web.app.test_client().get("/sessions").get_data(as_text=True)
    assert outline in html


def test_rows_fragment_same_structure(monkeypatch):
    sessions = [_full_session(id=f"sess-{i:04d}") for i in range(55)]
    monkeypatch.setattr(web, "load_index", lambda: _index_with(*sessions))
    frag = web.app.test_client().get("/sessions/rows?page=2").get_data(as_text=True)
    assert 'role="listitem"' in frag
    assert "row-menu" in frag
    assert 'href="/session/sess-' in frag
    assert "<html" not in frag.lower()


def test_filters_sheet_and_active_count(client):
    html = client.get("/sessions?tool=claude-code&start=2026-01-01").get_data(as_text=True)
    assert "filters-sheet" in html
    assert re.search(r"Filters\s*·\s*2", html)


def test_no_inline_style_on_delete_action(client):
    html = client.get("/sessions").get_data(as_text=True)
    form_chunk = html.split("/delete", 1)[0].rsplit("<form", 1)[-1]
    form_html = "<form" + form_chunk.split("</form>", 1)[0] + "</form>"
    assert "style=" not in form_html.lower()


@pytest.mark.parametrize(
    "title,outline,expected",
    [
        (None, None, False),
        ("Hello", "", False),
        ("Hello", "   ", False),
        ("Fix N+1 query…", "Fix N+1 query please take a look", False),
        ("Title", "Something else entirely", True),
        ("Title here", "Body text elsewhere", True),
    ],
)
def test_should_show_prompt_outline_helper(title, outline, expected):
    assert should_show_prompt_outline(title, outline) is expected


def test_should_show_prompt_outline_punctuation_only_prefix():
    assert should_show_prompt_outline("Hello, world!", "Hello world — more text") is False


def test_mixed_naive_and_aware_timestamps_do_not_break_the_row_helpers():
    from lore.interfaces.web_formatting import format_session_duration, format_session_row_time

    mixed = format_session_duration("2026-04-01T10:00:00", "2026-04-01T11:05:00Z")
    assert isinstance(mixed, str)  # no TypeError; the value depends on the local timezone
    assert format_session_duration("2026-04-01T10:00:00Z", "2026-04-01T11:05:00Z") == "1h 5m"
    label, attr = format_session_row_time("2026-04-01T00:30:00+02:00")
    assert label and attr
    assert format_session_duration("not a date", "2026-04-01T11:05:00") == ""


def test_negative_tokens_render_no_orphan_label(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with(_full_session(tokens=-1)))
    html = web.app.test_client().get("/sessions").get_data(as_text=True)
    assert "-1 tokens" not in html
    assert ">tokens<" not in html.replace(" ", "")


def test_filter_sheet_is_opened_on_desktop_widths(client):
    html = client.get("/sessions").get_data(as_text=True)

    assert "matchMedia('(min-width: 1024px)')" in html
    assert "sheet.setAttribute('open', '')" in html
