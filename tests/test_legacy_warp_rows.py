"""Sessions ingested from the removed Warp extractor must keep loading and rendering."""

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.web_data import _tool_from_value
from lore.interfaces.web_helpers import get_style
from lore.interfaces.web_services import normalize_session_messages_for_display

_ENTRY = {
    "id": "ses-legacy-warp-1",
    "tool": "warp",
    "title": "legacy warp title",
    "created": "2026-03-01T10:00:00",
    "updated": "2026-03-01T10:01:00",
    "messages": 1,
    "prompts": 1,
    "project": "/repo",
    "thread_id": "thread-legacy",
    "export_path": None,
}


def test_legacy_tool_value_loads_and_styles_as_plain_string():
    assert _tool_from_value("warp") is Tool.WARP
    assert get_style("warp")["name"] == "warp"


def test_legacy_session_display_normalization_leaves_content_alone():
    session = UnifiedSession(
        tool=Tool.WARP,
        session_id="ses-legacy-warp-2",
        created_at=datetime(2026, 3, 1),
        last_updated=datetime(2026, 3, 1),
        messages=[
            UnifiedMessage(
                role=Role.ASSISTANT, content="toolu_abc123", timestamp=datetime(2026, 3, 1)
            )
        ],
    )
    normalize_session_messages_for_display(session)
    assert session.messages[0].content == "toolu_abc123"


def test_sessions_page_and_filter_render_legacy_row(monkeypatch):
    idx = {"sessions": [_ENTRY], "stats": {"by_tool": {"warp": 1}}}
    monkeypatch.setattr(web, "load_index", lambda: idx)
    with web.app.test_client() as client:
        for url in ("/sessions", "/sessions?tool=warp"):
            response = client.get(url)
            assert response.status_code == 200, url
            assert "legacy warp title" in response.get_data(as_text=True), url
