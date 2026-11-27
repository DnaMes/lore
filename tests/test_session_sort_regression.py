"""Regression tests for session sort order.

Ensures recent-activity sort uses `updated` (last activity) and falls back
to `created`. Sessions that span days/weeks (long-running Claude Code
projects) must surface near the top when they have recent activity, even
if they were originally created weeks ago.
"""

from __future__ import annotations

from annuli.interfaces import web


def _make_session(sid: str, created: str, updated: str | None = None) -> dict:
    return {
        "id": sid,
        "tool": "claude-code",
        "title": sid,
        "project": "/tmp/x",
        "thread_id": f"t-{sid}",
        "messages": 1,
        "prompts": 1,
        "created": created,
        "updated": updated,
        "search_text": sid,
    }


def test_dashboard_recent_sorted_by_updated(monkeypatch):
    """Long-running session created earlier but updated recently should rank first."""
    sessions = [
        _make_session(
            "old-but-active",
            created="2026-03-24T09:00:00+00:00",
            updated="2026-05-06T04:00:00+00:00",
        ),
        _make_session(
            "new-but-quiet",
            created="2026-05-05T09:00:00+00:00",
            updated="2026-05-05T09:05:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": sessions,
            "stats": {
                "total_sessions": 2,
                "total_messages": 0,
                "by_tool": {},
                "by_project": {},
            },
        },
    )
    with web.app.test_client() as client:
        response = client.get("/")

    body = response.get_data(as_text=True)
    pos_old = body.find("old-but-active")
    pos_new = body.find("new-but-quiet")
    assert pos_old != -1, "active session must be in dashboard recents"
    assert pos_new != -1, "newer-but-quiet session must be in dashboard recents"
    assert pos_old < pos_new, (
        "session with newer 'updated' must appear before session with newer 'created'"
    )


def test_sessions_list_sorted_by_updated(monkeypatch):
    sessions = [
        _make_session(
            "old-but-active",
            created="2026-03-24T09:00:00+00:00",
            updated="2026-05-06T04:00:00+00:00",
        ),
        _make_session(
            "new-but-quiet",
            created="2026-05-05T09:00:00+00:00",
            updated="2026-05-05T09:05:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": sessions,
            "stats": {
                "total_sessions": 2,
                "total_messages": 0,
                "by_tool": {},
                "by_project": {},
            },
        },
    )
    with web.app.test_client() as client:
        response = client.get("/sessions")

    body = response.get_data(as_text=True)
    pos_old = body.find("old-but-active")
    pos_new = body.find("new-but-quiet")
    assert pos_old != -1
    assert pos_new != -1
    assert pos_old < pos_new


def test_sort_falls_back_to_created_when_updated_missing(monkeypatch):
    sessions = [
        _make_session("a-no-updated", created="2026-05-01T09:00:00+00:00", updated=None),
        _make_session("b-no-updated", created="2026-05-02T09:00:00+00:00", updated=None),
    ]
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": sessions,
            "stats": {
                "total_sessions": 2,
                "total_messages": 0,
                "by_tool": {},
                "by_project": {},
            },
        },
    )
    with web.app.test_client() as client:
        response = client.get("/")

    body = response.get_data(as_text=True)
    assert body.find("b-no-updated") < body.find("a-no-updated")
