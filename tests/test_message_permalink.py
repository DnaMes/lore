"""Message permalinks and bounded transcript loading (#171).

The session detail page prepared the whole transcript before slicing a page, and
the messages API serialised the entire live session and truncated afterwards.
Both now read a bounded window from the v2 message rows; a deep link is the pair
``(session_id, seq)``, which survives a reindex, a prune/undo and a restore.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.storage import load_session_message_window, v2_db_path, write_sessions

SESSION_ID = "ses_permalink"
TOTAL = 200


def _session(session_id: str = SESSION_ID, total: int = TOTAL) -> UnifiedSession:
    start = datetime(2026, 1, 1, 8, 0, 0)
    messages = []
    for index in range(total):
        role = Role.USER if index % 2 == 0 else Role.ASSISTANT
        messages.append(
            UnifiedMessage(
                role=role,
                content=f"message body {index + 1}",
                timestamp=start + timedelta(minutes=index),
            )
        )
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=start,
        last_updated=start + timedelta(minutes=total),
        project_path="/repo/lore",
        title="Permalink session",
        messages=messages,
    )


def _index_entry(session: UnifiedSession) -> dict:
    return {
        "id": session.session_id,
        "tool": session.tool.value,
        "title": session.title,
        "display_title": session.title,
        "created": session.created_at.isoformat(),
        "updated": session.last_updated.isoformat(),
        "project": session.project_path,
        "messages": len(session.messages),
        "prompts": 100,
    }


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """A v2 store with a 200-message session plus the index entry the routes read."""
    session = _session()
    write_sessions(v2_db_path(tmp_path), [session])
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"sessions": [_index_entry(session)], "stats": {}}), encoding="utf-8"
    )
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    with web.app.test_client() as client:
        yield tmp_path, client


# ---------------------------------------------------------------------------
# the reader primitive
# ---------------------------------------------------------------------------


def test_window_returns_the_requested_segment(archive):
    tmp_path, _client = archive
    window = load_session_message_window(tmp_path, SESSION_ID, offset=10, limit=5)
    assert window["total"] == TOTAL
    # Stored seq starts at 0: content "message body 11" is seq 10.
    assert [message["seq"] for message in window["messages"]] == [10, 11, 12, 13, 14]
    assert window["messages"][0]["content"] == "message body 11"


def test_window_never_loads_more_than_the_limit(archive):
    tmp_path, _client = archive
    window = load_session_message_window(tmp_path, SESSION_ID, offset=0, limit=3)
    assert len(window["messages"]) == 3


def test_window_centres_on_a_message(archive):
    tmp_path, _client = archive
    window = load_session_message_window(tmp_path, SESSION_ID, limit=5, around_seq=100)
    sequences = [message["seq"] for message in window["messages"]]
    assert 100 in sequences
    assert window["found_seq"] == 100
    assert sequences == [98, 99, 100, 101, 102]


def test_window_around_the_first_message_does_not_go_negative(archive):
    tmp_path, _client = archive
    window = load_session_message_window(tmp_path, SESSION_ID, limit=5, around_seq=1)
    assert window["offset"] == 0
    assert [message["seq"] for message in window["messages"]] == [0, 1, 2, 3, 4]


def test_window_reports_a_missing_message(archive):
    tmp_path, _client = archive
    window = load_session_message_window(tmp_path, SESSION_ID, limit=5, around_seq=9999)
    assert window["found_seq"] is None
    assert window["total"] == TOTAL


def test_window_for_an_unknown_session_is_none(archive):
    tmp_path, _client = archive
    assert load_session_message_window(tmp_path, "ses_missing", limit=5) is None


# ---------------------------------------------------------------------------
# paged API
# ---------------------------------------------------------------------------


def test_api_returns_a_bounded_page(archive):
    _tmp_path, client = archive
    response = client.get(f"/api/v1/sessions/{SESSION_ID}/messages?page=2&per_page=10")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == TOTAL
    assert payload["page"] == 2
    assert [message["seq"] for message in payload["messages"]] == list(range(10, 20))
    assert payload["next_page"] == 3
    assert payload["source"] == "v2"


def test_api_last_page_has_no_next_page(archive):
    _tmp_path, client = archive
    response = client.get(f"/api/v1/sessions/{SESSION_ID}/messages?page=20&per_page=10")
    payload = response.get_json()
    assert [message["seq"] for message in payload["messages"]] == list(range(190, 200))
    assert "next_page" not in payload


def test_api_at_parameter_centres_the_page(archive):
    _tmp_path, client = archive
    response = client.get(f"/api/v1/sessions/{SESSION_ID}/messages?at=150&per_page=11")
    payload = response.get_json()
    sequences = [message["seq"] for message in payload["messages"]]
    assert 150 in sequences
    assert min(sequences) == 145


def test_api_messages_carry_a_permalink(archive):
    _tmp_path, client = archive
    payload = client.get(f"/api/v1/sessions/{SESSION_ID}/messages?page=1&per_page=2").get_json()
    assert payload["messages"][0]["permalink"] == f"/session/{SESSION_ID}/m/0"


def test_api_unknown_session_is_404(archive):
    _tmp_path, client = archive
    assert client.get("/api/v1/sessions/ses_missing/messages").status_code == 404


def test_api_does_not_materialise_the_transcript(archive, monkeypatch):
    """A bounded page must not go through the live/exports parse."""
    _tmp_path, client = archive

    def explode(*_args, **_kwargs):
        raise AssertionError("the live session path must not be used for a bounded page")

    monkeypatch.setattr(web, "load_session_by_id", explode)
    response = client.get(f"/api/v1/sessions/{SESSION_ID}/messages?page=3&per_page=10")
    assert response.status_code == 200
    assert response.get_json()["source"] == "v2"


def test_api_rejects_bad_paging_parameters(archive):
    _tmp_path, client = archive
    assert client.get(f"/api/v1/sessions/{SESSION_ID}/messages?page=0").status_code == 400
    assert client.get(f"/api/v1/sessions/{SESSION_ID}/messages?at=abc").status_code == 400


# ---------------------------------------------------------------------------
# single-message permalink
# ---------------------------------------------------------------------------


def test_api_message_permalink_returns_the_message_and_context(archive):
    _tmp_path, client = archive
    payload = client.get(f"/api/v1/sessions/{SESSION_ID}/messages/42").get_json()
    assert payload["seq"] == 42
    assert payload["message"]["content"] == "message body 43"
    assert 42 in [message["seq"] for message in payload["context"]]
    assert payload["permalink"] == f"/session/{SESSION_ID}/m/42"


def test_api_message_permalink_for_a_deleted_message_is_410(archive):
    _tmp_path, client = archive
    response = client.get(f"/api/v1/sessions/{SESSION_ID}/messages/9999")
    assert response.status_code == 410
    body = response.get_json()
    assert body["error"] == "Message not found"
    assert body["seq"] == 9999


def test_api_message_permalink_unknown_session_is_404(archive):
    _tmp_path, client = archive
    assert client.get("/api/v1/sessions/ses_missing/messages/1").status_code == 404


def test_html_permalink_renders_only_the_window(archive):
    _tmp_path, client = archive
    response = client.get(f"/session/{SESSION_ID}/m/42")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="msg-42"' in html
    assert "message body 43" in html
    assert "Message 42 of 200" in html
    # Neighbours are present, the rest of the transcript is not.
    assert "message body 33" in html
    assert "message body 120" not in html


def test_html_permalink_for_a_missing_message_is_410(archive):
    _tmp_path, client = archive
    response = client.get(f"/session/{SESSION_ID}/m/9999")
    assert response.status_code == 410
    assert "not available" in response.get_data(as_text=True)


def test_html_permalink_unknown_session_is_404(archive):
    _tmp_path, client = archive
    response = client.get("/session/ses_missing/m/1")
    assert response.status_code == 404


def test_permalink_identity_is_the_stored_seq(archive):
    """Reindexing rewrites derived rows; (session_id, seq) keeps resolving."""
    tmp_path, client = archive
    from lore.exporters.index import IndexBuilder

    IndexBuilder(tmp_path).build_index([_session()], {})
    assert client.get(f"/api/v1/sessions/{SESSION_ID}/messages/42").status_code == 200
