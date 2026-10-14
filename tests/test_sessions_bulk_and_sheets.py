"""B5.6 — mobile bottom sheets, bulk select, bulk delete/tag."""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.interfaces.web_formatting import format_message_content, format_tool_calls
from lore.interfaces.web_services import enrich_session_for_detail
from lore.storage.summaries import get_summary, save_summary
from lore.utils.text_processing import format_thinking

_DETAIL_ID = "sess-b56-detail"
_LIST_ID = "sess-b56-list01"
_LIST_ID_2 = "sess-b56-list02"


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


def _list_session(session_id: str, **overrides) -> dict:
    base = {
        "id": session_id,
        "tool": "claude-code",
        "title": f"Title for {session_id}",
        "display_title": f"Title for {session_id}",
        "created": "2026-04-01T10:00:00",
        "updated": "2026-04-01T11:00:00",
        "project": "/repo/lore",
        "messages": 4,
        "prompts": 2,
        "tokens": 1000,
    }
    base.update(overrides)
    return base


def _detail_index() -> dict:
    return _index_with(
        {
            "id": _DETAIL_ID,
            "tool": "claude-code",
            "title": "Detail with prompts",
            "display_title": "Detail with prompts",
            "created": "2026-04-01T10:00:00",
            "updated": "2026-04-01T10:30:00",
            "project": "/repo/lore",
            "messages": 4,
            "prompts": 2,
            "tokens": 500,
        }
    )


def _synthetic_detail_session() -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=_DETAIL_ID,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 30, 0),
        project_path="/repo/lore",
        title="Detail with prompts",
        messages=[
            UnifiedMessage(Role.USER, "First user prompt here", datetime(2026, 4, 1, 10, 0, 0)),
            UnifiedMessage(Role.ASSISTANT, "First reply", datetime(2026, 4, 1, 10, 1, 0)),
            UnifiedMessage(Role.USER, "Second user prompt", datetime(2026, 4, 1, 10, 2, 0)),
            UnifiedMessage(Role.ASSISTANT, "Second reply", datetime(2026, 4, 1, 10, 3, 0)),
        ],
    )


@pytest.fixture()
def list_client(monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_with(_list_session(_LIST_ID), _list_session(_LIST_ID_2)),
    )
    return web.app.test_client()


@pytest.fixture()
def detail_client(monkeypatch):
    session = _synthetic_detail_session()
    enrich_session_for_detail(
        session,
        _detail_index()["sessions"][0],
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    toc = [
        {"index": 0, "text": "First user prompt here", "time": "10:00"},
        {"index": 2, "text": "Second user prompt", "time": "10:02"},
    ]
    monkeypatch.setattr(web, "load_index", lambda: _detail_index())
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, toc),
    )
    return web.app.test_client()


class _TocLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.toc_hrefs: list[str] = []
        self.in_desktop_toc = False
        self.in_outline_dialog = False
        self.outline_hrefs: list[str] = []
        self._dialog_depth = 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "aside":
            self.in_desktop_toc = True
        if attr.get("id") == "promptOutlineSheet":
            self.in_outline_dialog = True
            self._dialog_depth = 1
        if tag == "a" and attr.get("href", "").startswith("#msg-"):
            classes = attr.get("class") or ""
            if "outline-sheet-link" in classes:
                self.outline_hrefs.append(attr["href"])
            elif self.in_desktop_toc and "outline-sheet-link" not in classes:
                self.toc_hrefs.append(attr["href"])

    def handle_endtag(self, tag):
        if tag == "aside":
            self.in_desktop_toc = False
        if tag == "div" and self.in_outline_dialog:
            self._dialog_depth -= 1
            if self._dialog_depth <= 0:
                self.in_outline_dialog = False


def test_detail_prompt_outline_sheet_markup(detail_client):
    html = detail_client.get(f"/session/{_DETAIL_ID}?live=1").get_data(as_text=True)
    assert 'aria-haspopup="dialog"' in html
    assert "Prompts" in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-label="Prompt outline"' in html
    assert (
        re.search(
            r'role="dialog"[^>]*aria-label="Prompt outline"[^>]*\bhidden\b',
            html,
        )
        or 'id="promptOutlineSheet"' in html
        and " hidden" in html
    )

    parser = _TocLinkParser()
    parser.feed(html)
    assert len(parser.toc_hrefs) == 2
    assert parser.outline_hrefs == parser.toc_hrefs


def test_list_filters_sheet_trigger_and_single_filter_form(list_client):
    html = list_client.get("/sessions").get_data(as_text=True)
    assert re.search(r"Filters\s*·", html)
    assert 'aria-haspopup="dialog"' in html
    assert html.count('<form method="get"') == 1
    assert 'aria-label="Filters"' in html or 'aria-label="Session filters"' in html


def test_list_row_checkboxes_outside_link_with_aria_label(list_client):
    html = list_client.get("/sessions").get_data(as_text=True)
    assert 'type="checkbox"' in html
    assert 'aria-label="Select Title for' in html
    row_start = html.find(f'data-session-id="{_LIST_ID}"')
    row_end = html.find('role="listitem"', row_start + 1)
    if row_end == -1:
        row_end = row_start + 1200
    row_chunk = html[row_start:row_end]
    cb_pos = row_chunk.find('type="checkbox"')
    link_pos = row_chunk.find(f'href="/session/{_LIST_ID}')
    assert cb_pos != -1 and link_pos != -1 and cb_pos < link_pos


def test_bulk_delete_tombstones_and_sanitises_next(monkeypatch, tmp_path):
    fake_output = tmp_path / ".ai-history"
    fake_output.mkdir()
    index_path = fake_output / "index.json"
    deleted_path = fake_output / "deleted_sessions.json"
    export_a = fake_output / "a.md"
    export_a.write_text("# a", encoding="utf-8")
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 2,
                    "total_messages": 2,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [
                    {
                        "id": _LIST_ID,
                        "tool": "claude-code",
                        "messages": 1,
                        "export_path": str(export_a),
                    },
                    {"id": _LIST_ID_2, "tool": "claude-code", "messages": 1, "export_path": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-delete",
            data={
                "session_id": [_LIST_ID],
                "next": "https://evil.example/phish",
            },
        )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/sessions"
    payload = json.loads(deleted_path.read_text(encoding="utf-8"))
    assert _LIST_ID in payload["session_ids"]
    assert not export_a.exists()


def test_bulk_delete_empty_list_400():
    with web.app.test_client() as client:
        resp = client.post("/sessions/bulk-delete", data={"next": "/sessions"})
    assert resp.status_code == 400


def test_bulk_delete_invalid_id_no_partial(monkeypatch, tmp_path):
    fake_output = tmp_path / ".ai-history"
    fake_output.mkdir()
    index_path = fake_output / "index.json"
    deleted_path = fake_output / "deleted_sessions.json"
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 1,
                    "total_messages": 1,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [{"id": _LIST_ID, "tool": "claude-code", "messages": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-delete",
            data={"session_id": [_LIST_ID, "bad;id"], "next": "/sessions"},
        )
    assert resp.status_code == 400
    if deleted_path.exists():
        assert _LIST_ID not in json.loads(deleted_path.read_text())["session_ids"]


def test_bulk_delete_more_than_200_ids_400():
    ids = [f"sess-b56-{i:04d}" for i in range(201)]
    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-delete",
            data={"session_id": ids, "next": "/sessions"},
        )
    assert resp.status_code == 400


def test_bulk_delete_get_not_allowed():
    with web.app.test_client() as client:
        resp = client.get("/sessions/bulk-delete")
    assert resp.status_code == 405


def test_bulk_delete_rejects_cross_origin():
    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-delete",
            data={"session_id": [_LIST_ID]},
            headers={"Origin": "http://evil.example.com"},
        )
    assert resp.status_code == 403


def test_bulk_delete_purges_summaries(monkeypatch, tmp_path):
    from lore.storage import initialise
    from lore.storage.summaries import summary_fingerprint
    from lore.storage.writer import v2_db_path

    fake_output = tmp_path / ".ai-history"
    fake_output.mkdir()
    initialise(v2_db_path(fake_output))
    index_path = fake_output / "index.json"
    deleted_path = fake_output / "deleted_sessions.json"
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 1,
                    "total_messages": 1,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [{"id": _LIST_ID, "tool": "claude-code", "messages": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()
    save_summary(
        fake_output,
        level="session",
        scope_key=f"session:{_LIST_ID}",
        session_id=_LIST_ID,
        fingerprint=summary_fingerprint("text", "fake/model", 1, 1),
        model="fake/model",
        prompt_version=1,
        compressor_version=1,
        payload={"summary": "x", "cites": []},
    )
    scope = f"session:{_LIST_ID}"
    assert get_summary(fake_output, "session", scope) is not None

    with web.app.test_client() as client:
        client.post("/sessions/bulk-delete", data={"session_id": [_LIST_ID], "next": "/sessions"})

    assert get_summary(fake_output, "session", scope) is None


def test_bulk_tag_applies_tag(monkeypatch, tmp_path):
    from lore.storage import get_session_tags, initialise
    from lore.storage.writer import v2_db_path

    fake_output = tmp_path / ".ai-history"
    fake_output.mkdir()
    initialise(v2_db_path(fake_output))
    index_path = fake_output / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 2,
                    "total_messages": 2,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [
                    {"id": _LIST_ID, "tool": "claude-code", "messages": 1},
                    {"id": _LIST_ID_2, "tool": "claude-code", "messages": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-tag",
            data={"session_id": [_LIST_ID, _LIST_ID_2], "tag": "reviewed", "next": "/sessions"},
        )
    assert resp.status_code == 302
    assert "reviewed" in get_session_tags(fake_output, _LIST_ID)
    assert "reviewed" in get_session_tags(fake_output, _LIST_ID_2)


def test_bulk_tag_invalid_tag_400_no_partial(monkeypatch, tmp_path):
    from lore.storage import get_session_tags

    fake_output = tmp_path / ".ai-history"
    fake_output.mkdir()
    index_path = fake_output / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 1,
                    "total_messages": 1,
                    "by_tool": {},
                    "by_project": {},
                },
                "sessions": [{"id": _LIST_ID, "tool": "claude-code", "messages": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        resp = client.post(
            "/sessions/bulk-tag",
            data={"session_id": [_LIST_ID], "tag": "<bad>", "next": "/sessions"},
        )
    assert resp.status_code == 400
    assert get_session_tags(fake_output, _LIST_ID) == []


def test_list_scripts_have_key_guards_and_no_confirm(list_client):
    html = list_client.get("/sessions").get_data(as_text=True)
    scripts = re.findall(r"<script[^>]*nonce=[^>]*>(.*?)</script>", html, re.DOTALL)
    combined = "\n".join(scripts)
    assert "ctrlKey" in combined
    assert "metaKey" in combined
    assert "altKey" in combined
    assert "isContentEditable" in combined
    assert "confirm(" not in combined
    assert "window.confirm" not in combined
    assert "FocusTrap" in combined
    assert "Escape" in combined
    assert re.search(r"<[^>]+\bon\w+\s*=", combined) is None


def test_detail_scripts_have_key_guards_and_focustrap(detail_client):
    html = detail_client.get(f"/session/{_DETAIL_ID}?live=1").get_data(as_text=True)
    scripts = re.findall(r"<script[^>]*nonce=[^>]*>(.*?)</script>", html, re.DOTALL)
    combined = "\n".join(scripts)
    assert "FocusTrap" in combined
    assert "Escape" in combined
    assert "confirm(" not in combined


def test_rows_are_bound_once_and_escape_closes_every_dialog():
    from pathlib import Path

    page = Path("lore/templates/sessions.html").read_text(encoding="utf-8")
    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert "if (row.dataset.boundSelect) return;" in page
    assert "[bulkDeleteConfirm, bulkTagDialog].forEach" in page
    assert "e.key !== 'Escape'" in base and "closeConfirmDialog();" in base
