"""The session page must put dynamic values into HTML attributes losslessly (#197 B0.1).

``data-session-id`` used to be rendered through ``|tojson`` inside a double-quoted
attribute, which produced ``data-session-id=""id""`` and broke the tag UI.
"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web

SESSION_IDS = ["session-1", 'a"b', "a'b", "a<b>c", "a&amp;b&c", "q\"'<&>x"]


class _AttrCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.values.extend((tag, value) for name, value in attrs if name == "data-session-id")


def _render_session_page(session_id: str) -> str:
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/demo",
        title="Attr test",
        messages=[UnifiedMessage(Role.USER, "hello", datetime(2026, 4, 1, 10, 0, 0))],
    )
    with web.app.test_request_context("/session/x"):
        return web.render(
            "session",
            active="sessions",
            session=session,
            visible_pairs=[],
            total_pairs=0,
            message_pages=1,
            messages_per_page=40,
            style=web.get_style("claude-code"),
            title="Attr test",
            toc_items=[],
            back_target="/sessions",
        )


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_data_session_id_attribute_round_trips(session_id):
    body = _render_session_page(session_id)
    parser = _AttrCollector()
    parser.feed(body)
    assert parser.values, "no data-session-id attribute rendered"
    assert all(value == session_id for _tag, value in parser.values)
    # An empty attribute (nonce="") is fine; a doubled quote glued to a value is not.
    assert not re.search(r'=""[^\s>/]', body)


def test_tag_script_reads_session_id_as_plain_string():
    body = _render_session_page("session-1")
    assert "JSON.parse(root.getAttribute('data-session-id'))" not in body
    assert "root.dataset.sessionId" in body
