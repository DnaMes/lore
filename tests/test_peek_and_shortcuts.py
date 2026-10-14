"""Peek pane (B5.7b) and keyboard shortcuts (B5.7c)."""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.web_formatting import format_message_content, format_tool_calls
from lore.interfaces.web_services import enrich_session_for_detail
from lore.services.summarize import SUMMARY_PROMPT_VERSION
from lore.storage import initialise
from lore.storage.summaries import save_summary, summary_fingerprint
from lore.storage.writer import v2_db_path, write_sessions
from lore.utils.text_processing import format_thinking

_PEEK_ID = "peek0001-0001-0001-0001-000000000001"
_PEEK_ID_2 = "peek0002-0002-0002-0002-000000000002"
_INVALID_ID = "not valid!"
_MODEL = "ollama/llama3.2"


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


def _meta(session_id: str = _PEEK_ID, **overrides) -> dict:
    base = {
        "id": session_id,
        "tool": "claude-code",
        "title": "Peek session title",
        "display_title": "Peek session title",
        "created": "2026-04-01T10:00:00",
        "updated": "2026-04-01T11:00:00",
        "project": "/repo/lore",
        "messages": 6,
        "prompts": 2,
        "tokens": 900,
        "prompt_outline": "First user prompt for peek",
    }
    base.update(overrides)
    return base


def _session(
    session_id: str = _PEEK_ID, user_text: str = "First user prompt for peek"
) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 11, 0, 0),
        project_path="/repo/lore",
        title="Peek session title",
        messages=[
            UnifiedMessage(Role.USER, user_text, datetime(2026, 4, 1, 10, 0, 0)),
            UnifiedMessage(Role.ASSISTANT, "ok", datetime(2026, 4, 1, 10, 5, 0)),
        ],
    )


def _enriched(session: UnifiedSession) -> UnifiedSession:
    enrich_session_for_detail(
        session,
        _meta(session.session_id),
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    return session


def _save_summary(tmp_path, session_id: str, one_liner: str) -> None:
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{session_id}",
        session_id=session_id,
        fingerprint=summary_fingerprint("body", _MODEL, SUMMARY_PROMPT_VERSION, 1),
        model=_MODEL,
        prompt_version=SUMMARY_PROMPT_VERSION,
        compressor_version=1,
        payload={"one_liner": one_liner, "goal": {"text": "g", "cites": []}},
    )


@pytest.fixture()
def peek_client(monkeypatch):
    session = _enriched(_session())
    monkeypatch.setattr(web, "load_index", lambda: _index_with(_meta()))
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, []),
    )
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: set())
    return web.app.test_client()


@pytest.fixture()
def list_client(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with(_meta(), _meta(_PEEK_ID_2)))
    return web.app.test_client()


def test_peek_fragment_has_title_and_counts(peek_client):
    resp = peek_client.get(f"/sessions/{_PEEK_ID}/peek")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/html")
    html = resp.get_data(as_text=True)
    assert "Peek session title" in html
    assert "prompts" in html.lower()
    assert 'class="peek-glance-stats"' in html or "summary-glance-stats" in html


def test_peek_escapes_html_in_title_and_prompt(peek_client, monkeypatch):
    evil = '<img onerror="alert(1)">'
    session = _enriched(_session(user_text=evil))
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(_meta(title=evil, display_title=evil, prompt_outline=evil)),
    )
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, []),
    )
    html = peek_client.get(f"/sessions/{_PEEK_ID}/peek").get_data(as_text=True)
    assert "<img" not in html
    assert "&lt;img" in html


def test_peek_shows_summary_one_liner_when_stored(peek_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    _save_summary(tmp_path, _PEEK_ID, "One line from AI summary")
    html = peek_client.get(f"/sessions/{_PEEK_ID}/peek").get_data(as_text=True)
    assert "One line from AI summary" in html
    assert "peek-summary-oneliner" in html


def test_peek_shows_at_a_glance_without_summary(peek_client):
    html = peek_client.get(f"/sessions/{_PEEK_ID}/peek").get_data(as_text=True)
    assert "First user prompt for peek" in html
    assert "peek-glance" in html


def test_peek_404_unknown_and_tombstoned(peek_client, monkeypatch):
    assert peek_client.get(f"/sessions/{_PEEK_ID_2}/peek").status_code == 404
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: {_PEEK_ID})
    assert peek_client.get(f"/sessions/{_PEEK_ID}/peek").status_code == 404


def test_peek_400_invalid_id(peek_client):
    assert peek_client.get(f"/sessions/{_INVALID_ID}/peek").status_code == 400


def test_peek_cache_control(peek_client):
    resp = peek_client.get(f"/sessions/{_PEEK_ID}/peek")
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=30" in cc


def test_list_page_has_peek_aside_and_media_query(list_client):
    html = list_client.get("/sessions").get_data(as_text=True)
    assert 'role="complementary"' in html
    assert 'aria-label="Session preview"' in html
    assert "@media (min-width: 1280px)" in html
    assert "lore.peek.v1" in html
    assert re.search(r"lore\.peek\.v1.*['\"]on['\"]|['\"]off['\"]", html)


def test_help_dialog_on_list_and_detail(list_client, peek_client):
    list_html = list_client.get("/sessions").get_data(as_text=True)
    detail_html = peek_client.get(f"/session/{_PEEK_ID}?live=1").get_data(as_text=True)
    for html in (list_html, detail_html):
        assert 'id="shortcutsHelpDialog"' in html
        assert 'aria-modal="true"' in html
        assert "data-shortcuts-list" in html
        assert "data-shortcuts-detail" in html


def _script_sources(html: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL))


def test_shared_lore_key_guard_once(list_client, peek_client):
    list_src = _script_sources(list_client.get("/sessions").get_data(as_text=True))
    detail_src = _script_sources(
        peek_client.get(f"/session/{_PEEK_ID}?live=1").get_data(as_text=True)
    )
    combined = list_src + detail_src
    assert list_src.count("function loreKeyGuard(") == 1
    assert detail_src.count("function loreKeyGuard(") == 1
    guard_chunk = combined.split("function loreKeyGuard(", 1)[1].split("};", 1)[0]
    for needle in (
        "ctrlKey",
        "metaKey",
        "altKey",
        "isContentEditable",
        '[aria-modal="true"].is-open',
    ):
        assert needle in guard_chunk
    assert "function keyGuard(" not in combined
    list_html = list_client.get("/sessions").get_data(as_text=True)
    assert not re.search(r"<\w+[^>]*\bon\w+\s*=", list_html)
    assert "window.confirm" not in combined
    assert "lore.keys.v1" in combined


class _TagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, _val in attrs:
            if name.startswith("on"):
                self.inline_handlers.append(name)


def test_no_inline_event_handlers_on_list(list_client):
    parser = _TagParser()
    parser.feed(list_client.get("/sessions").get_data(as_text=True))
    assert parser.inline_handlers == []


def test_j_without_a_hash_selects_the_first_prompt():
    from pathlib import Path

    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert "let idx = hash ? ids.indexOf(hash) : -1;" in base
    assert "if (idx < 0) idx = 0;" not in base  # that line made j skip the first prompt
    assert "Math.max(0, idx < 0 ? 0 : idx - 1)" in base
