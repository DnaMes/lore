"""Golden render snapshots for every page route (#30 safety net).

Captures the rendered HTML of each route so the Jinja-template extraction
(strings in web_templates.py → lore/templates/*.html) can be proven
output-neutral. The per-request CSP nonce is normalized out before
comparison since it varies by request.

Regenerate snapshots after an *intentional* template change with:

    UPDATE_TEMPLATE_SNAPSHOTS=1 pytest tests/test_template_render_snapshots.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "templates"

# Replace the per-request CSP nonce value so snapshots are request-stable.
_NONCE_RE = re.compile(r'nonce="[^"]*"')


def _normalize(html: str) -> str:
    return _NONCE_RE.sub('nonce="NONCE"', html)


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


# Page routes that render a full template (name used only for the snapshot file).
PAGE_ROUTES = [
    ("dashboard", "/"),
    ("sessions", "/sessions"),
    ("projects", "/projects"),
    ("threads", "/threads"),
    ("memory", "/memory"),
    ("stats", "/stats"),
    ("rules", "/rules"),
    ("noise_rules", "/noise-rules"),
    ("session_rows", "/sessions/rows"),
]


# A fixed noise-rules payload so the /noise-rules snapshot does not depend on the
# machine's ~/.lore/noise_rules.json (which differs between a dev box and CI).
_FIXED_NOISE_RULES = {
    "default": {"strip_ansi_sequences": True},
    "claude-code": {"claude_strip_command_xml": True},
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_payload())
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [_live_session()])
    monkeypatch.setattr(web, "load_noise_rules", lambda: _FIXED_NOISE_RULES)
    # The sessions page embeds today's date in its quick-range links.
    monkeypatch.setattr(web, "sessions_list_now", lambda: datetime(2026, 10, 14, 12, 0, 0))
    with web.app.test_client() as c:
        yield c


def _assert_snapshot(name: str, html: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.html"
    normalized = _normalize(html)
    if os.environ.get("UPDATE_TEMPLATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(normalized, encoding="utf-8")
        if os.environ.get("UPDATE_TEMPLATE_SNAPSHOTS") != "1":
            pytest.skip(f"created baseline snapshot {name}.html")
        return
    expected = path.read_text(encoding="utf-8")
    assert normalized == expected, f"render output changed for {name} (route snapshot mismatch)"


@pytest.mark.parametrize("name,route", PAGE_ROUTES)
def test_page_route_render_matches_snapshot(client, name, route):
    resp = client.get(route)
    assert resp.status_code == 200
    _assert_snapshot(name, resp.get_data(as_text=True))


def test_session_detail_renders_extends_and_include(client):
    """/session/<id> exercises extends base + include session_pairs."""
    resp = client.get("/session/session-1?live=1")
    assert resp.status_code == 200
    _assert_snapshot("session_detail", resp.get_data(as_text=True))


def test_thread_detail_renders(client):
    resp = client.get("/thread/thread-1")
    assert resp.status_code == 200
    _assert_snapshot("thread_detail", resp.get_data(as_text=True))


def test_nonce_is_normalized_so_snapshots_are_stable(client):
    """Two requests to the same route normalize to identical HTML."""
    a = _normalize(client.get("/").get_data(as_text=True))
    b = _normalize(client.get("/").get_data(as_text=True))
    assert a == b


def test_environment_built_once(monkeypatch):
    """R3 (#30) — the Jinja Environment is constructed once, not per render()."""
    from lore.interfaces import web as web_mod

    web_mod._template_env.cache_clear()
    calls = {"n": 0}
    import jinja2

    orig_env = jinja2.Environment

    def counting_env(*args, **kwargs):
        calls["n"] += 1
        return orig_env(*args, **kwargs)

    monkeypatch.setattr(jinja2, "Environment", counting_env)
    # Force a rebuild under the patched constructor, then render twice.
    web_mod._template_env.cache_clear()
    e1 = web_mod._template_env()
    e2 = web_mod._template_env()
    assert e1 is e2  # cached, same instance
    assert calls["n"] == 1  # constructed exactly once
    web_mod._template_env.cache_clear()  # leave a clean cache for other tests


def test_web_templates_module_is_gone():
    """#30 — the old string-template module must no longer exist."""
    import importlib.util

    assert importlib.util.find_spec("lore.interfaces.web_templates") is None
