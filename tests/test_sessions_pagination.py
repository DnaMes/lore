"""
Server-side pagination tests for the /sessions page (issue #17).

The /sessions route used to inline every session into one HTML response
(up to ~19 MB). It now renders only the first SESSIONS_PER_PAGE sessions and
exposes /sessions/rows for lazy "Load more" fragments.
"""

from __future__ import annotations

import pytest

from annuli.interfaces import web


def _make_index(count: int) -> dict:
    """Build an index with `count` synthetic sessions.

    Timestamps decrease with the index so that after the route's
    newest-first sort the sessions land in id order: sess-0000 first.
    """
    sessions = [
        {
            "id": f"sess-{i:04d}",
            "tool": "claude",
            "title": f"Session {i}",
            "display_title": f"Session {i}",
            "created": "2026-01-01T00:00:00Z",
            "updated": f"2026-{count - i:06d}",
            "project": "/tmp/proj",
            "messages": 3,
            "prompts": 1,
        }
        for i in range(count)
    ]
    return {
        "sessions": sessions,
        "stats": {
            "total_sessions": count,
            "total_messages": count * 3,
            "by_tool": {"claude": count},
            "by_project": {"/tmp/proj": count},
        },
        "generated_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture()
def client_with(monkeypatch):
    """Return a factory that yields a test client backed by N sessions."""

    def _factory(count: int):
        monkeypatch.setattr(web, "load_index", lambda: _make_index(count))
        return web.app.test_client()

    return _factory


def test_first_page_caps_rendered_sessions(client_with):
    client = client_with(130)
    body = client.get("/sessions").get_data(as_text=True)
    # Only the first page of session rows should be inlined.
    assert body.count('class="compact-row') == web.SESSIONS_PER_PAGE
    assert "sess-0000" in body
    assert "sess-0049" in body
    assert "sess-0050" not in body


def test_load_more_button_shown_when_multiple_pages(client_with):
    client = client_with(130)
    body = client.get("/sessions").get_data(as_text=True)
    assert 'id="loadMoreSessions"' in body
    assert "130 sessions" in body


def test_load_more_button_hidden_for_single_page(client_with):
    client = client_with(10)
    body = client.get("/sessions").get_data(as_text=True)
    assert 'id="loadMoreSessions"' not in body


def test_rows_fragment_returns_next_page(client_with):
    client = client_with(130)
    r = client.get("/sessions/rows?page=2")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Page 2 holds sessions 50..99; the surrounding <section>/base chrome must not.
    assert "sess-0050" in body
    assert "sess-0099" in body
    assert "sess-0049" not in body
    assert "<html" not in body.lower()


def test_rows_fragment_last_page_is_partial(client_with):
    client = client_with(130)
    body = client.get("/sessions/rows?page=3").get_data(as_text=True)
    # 130 sessions / 50 per page -> page 3 has 30 rows.
    assert body.count('class="compact-row') == 30


def test_rows_fragment_rejects_bad_page(client_with):
    client = client_with(10)
    assert client.get("/sessions/rows?page=0").status_code == 400
    assert client.get("/sessions/rows?page=abc").status_code == 400


def test_rows_fragment_rejects_bad_tool(client_with):
    client = client_with(10)
    assert client.get("/sessions/rows?page=1&tool=../etc").status_code == 400
