"""
Conversation pagination tests for the /session/<id> detail page.

The detail route used to inline every message of a session into one HTML
response (a 2571-message session produced an ~8 MB page). It now renders only
the first MESSAGES_PER_PAGE conversation pairs and exposes
/session/<id>/messages for lazy "Load more messages" fragments.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.interfaces import web


def _make_session(session_id: str, pair_count: int) -> UnifiedSession:
    """Build a UnifiedSession with `pair_count` user/assistant turn pairs."""
    messages: list[UnifiedMessage] = []
    ts = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(pair_count):
        messages.append(
            UnifiedMessage(role=Role.USER, content=f"Question number {i}", timestamp=ts)
        )
        messages.append(
            UnifiedMessage(role=Role.ASSISTANT, content=f"Answer number {i}", timestamp=ts)
        )
    return UnifiedSession(
        session_id=session_id,
        tool=Tool.CLAUDE_CODE,
        title="Big Session",
        messages=messages,
        created_at=ts,
        last_updated=ts,
        project_path="/tmp/proj",
    )


def _meta(session_id: str) -> dict:
    return {
        "id": session_id,
        "tool": "claude-code",
        "title": "Big Session",
        "created": "2026-01-01T12:00:00",
        "updated": "2026-01-01T12:00:00",
        "project": "/tmp/proj",
        "messages": 0,
        "prompts": 0,
    }


SESSION_ID = "abc12345-def6-7890-abcd-ef1234567890"


@pytest.fixture()
def client_with(monkeypatch):
    """Return a factory yielding a test client backed by a session of N pairs."""

    def _factory(pair_count: int, session_id: str = SESSION_ID):
        session = _make_session(session_id, pair_count)

        def _fake_index():
            return {
                "sessions": [_meta(session_id)],
                "stats": {
                    "total_sessions": 1,
                    "total_messages": pair_count * 2,
                    "by_tool": {"claude-code": 1},
                    "by_project": {"/tmp/proj": 1},
                },
            }

        def _fake_resolve(sid, *, force_live):
            if sid != session_id:
                return None
            toc = web.enrich_session_for_detail(
                session,
                _meta(session_id),
                web.format_message_content,
                web.format_tool_calls,
                web.format_thinking,
                noise_rules=None,
            )
            return session, toc

        monkeypatch.setattr(web, "load_index", _fake_index)
        monkeypatch.setattr(web, "_enriched_session_for_detail", _fake_resolve)
        return web.app.test_client()

    return _factory


def test_first_page_caps_rendered_messages(client_with):
    client = client_with(120)
    body = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    # Only the first MESSAGES_PER_PAGE pairs inline into the conversation column.
    assert body.count('class="message-cluster"') == web.MESSAGES_PER_PAGE
    # The TOC lists every prompt (lightweight text), but the heavy rendered
    # answer cards must stop after the first page.
    assert "Answer number 0" in body
    assert f"Answer number {web.MESSAGES_PER_PAGE - 1}" in body
    assert f"Answer number {web.MESSAGES_PER_PAGE}" not in body
    assert f"Answer number {web.MESSAGES_PER_PAGE + 5}" not in body


def test_toc_anchors_emitted_for_rendered_pairs(client_with):
    client = client_with(120)
    body = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    # The TOC anchors must still be emitted for every rendered pair.
    assert 'id="msg-0"' in body
    assert 'id="msg-2"' in body  # second user message is index 2


def test_load_more_button_shown_when_multiple_pages(client_with):
    client = client_with(120)
    body = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    assert 'id="loadMoreMessages"' in body
    assert 'id="messageStream"' in body


def test_load_more_button_hidden_for_single_page(client_with):
    client = client_with(10)
    body = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    assert 'id="loadMoreMessages"' not in body


def test_messages_fragment_returns_next_page(client_with):
    client = client_with(120)
    r = client.get(f"/session/{SESSION_ID}/messages?page=2")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Page 2 holds pairs 40..79; page-1 content and base chrome must not appear.
    assert f"Question number {web.MESSAGES_PER_PAGE}" in body
    assert f"Question number {web.MESSAGES_PER_PAGE * 2 - 1}" in body
    assert "Question number 0" not in body
    assert "<html" not in body.lower()


def test_messages_fragment_last_page_is_partial(client_with):
    client = client_with(95)
    body = client.get(f"/session/{SESSION_ID}/messages?page=3").get_data(as_text=True)
    # 95 pairs / 40 per page -> page 3 has 15 clusters.
    assert body.count('class="message-cluster"') == 95 - 2 * web.MESSAGES_PER_PAGE


def test_messages_fragment_continues_turn_index(client_with):
    client = client_with(120)
    body = client.get(f"/session/{SESSION_ID}/messages?page=2").get_data(as_text=True)
    # First turn badge on page 2 must continue from page 1 (offset 40 -> 41).
    assert f">{web.MESSAGES_PER_PAGE + 1}</span>" in body


def test_messages_fragment_rejects_bad_page(client_with):
    client = client_with(10)
    assert client.get(f"/session/{SESSION_ID}/messages?page=0").status_code == 400
    assert client.get(f"/session/{SESSION_ID}/messages?page=abc").status_code == 400


def test_messages_fragment_rejects_bad_session_id(client_with):
    client = client_with(10)
    assert client.get("/session/..%2Fetc/messages?page=1").status_code in (400, 404)


def test_messages_fragment_unknown_session_returns_404(client_with):
    client = client_with(10)
    other = "ffffffff-1111-2222-3333-444444444444"
    assert client.get(f"/session/{other}/messages?page=1").status_code == 404
