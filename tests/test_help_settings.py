"""Tests for the /help and /settings pages and simplified sidebar navigation."""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web


def _index_payload() -> dict:
    return {
        "sessions": [
            {
                "id": "session-1",
                "tool": "claude-code",
                "title": "Claude debugging session",
                "display_title": "Claude debugging session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/demo",
                "thread_id": "thread-1",
                "messages": 2,
                "prompts": 1,
                "tokens": 1234,
                "export_path": None,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo/demo": 1},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }


def _live_session() -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="session-1",
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/demo",
        thread_id="thread-1",
        title="Claude debugging session",
        messages=[
            UnifiedMessage(Role.USER, "Find the sqlite issue", datetime(2026, 4, 1, 10, 0, 0)),
            UnifiedMessage(
                Role.ASSISTANT, "I found the locking bug", datetime(2026, 4, 1, 10, 1, 0)
            ),
        ],
    )


_FIXED_NOISE_RULES = {
    "default": {"strip_ansi_sequences": True},
    "claude-code": {"claude_strip_command_xml": True},
}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    fake_output = tmp_path / ".annuli"
    fake_output.mkdir()
    fake_index = fake_output / "index.json"
    fake_index.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web, "INDEX_PATH", fake_index)
    monkeypatch.setattr(web, "load_index", lambda: _index_payload())
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [_live_session()])
    monkeypatch.setattr(web, "load_noise_rules", lambda: _FIXED_NOISE_RULES)
    monkeypatch.setattr(web, "sessions_list_now", lambda: datetime(2026, 10, 14, 12, 0, 0))
    with web.app.test_client() as c:
        yield c


def test_help_page_contains_glossary_and_shortcut(client):
    resp = client.get("/help")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for term in (
        "Session",
        "Prompt",
        "Project",
        "Thread",
        "Timeline",
        "Memory",
        "Candidate",
        "Automated session",
        "Rules",
        "Noise rules",
        "Library",
    ):
        assert term in body
    assert "Ctrl" in body


def test_settings_page_status_and_masked_api_key(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret123")
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "OPENROUTER_API_KEY" in body
    assert "set" in body
    assert "secret123" not in body


def test_sidebar_navigation_links(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    start = html.index("<aside")
    end = html.index("</aside>", start)
    aside = html[start:end]

    for href in (
        'href="/sessions"',
        'href="/projects"',
        'href="/memory"',
        'href="/stats"',
        'href="/settings"',
        'href="/help"',
    ):
        assert href in aside

    for hidden in ('href="/threads"', 'href="/rules"', 'href="/noise-rules"'):
        assert hidden not in aside


@pytest.mark.parametrize("route", ["/threads", "/rules", "/noise-rules"])
def test_secondary_routes_still_return_200(client, route):
    resp = client.get(route)
    assert resp.status_code == 200
