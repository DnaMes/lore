"""The session page builds from the v2 store before the lossy export markdown.

The export keeps tool results as "user" turns and times without dates, so a
session read from it showed every tool result as a prompt. The v2 store holds
roles, full timestamps and structured tool calls; it is the first source now,
and only ``?live=1`` touches the tool's own files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.interfaces.routes import sessions as sessions_routes
from lore.storage.schema import initialise
from lore.storage.writer import v2_db_path, write_sessions

SID = "sess-v2-view"
T0 = datetime(2026, 11, 7, 19, 58, 0, tzinfo=timezone.utc)


def _msg(role: Role, content: str, minutes: int, **kwargs) -> UnifiedMessage:
    return UnifiedMessage(
        role=role, content=content, timestamp=T0 + timedelta(minutes=minutes), **kwargs
    )


def _session() -> UnifiedSession:
    call = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Bash",
        "input": {"command": "pytest -q"},
        "output": "1 failed",
    }
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=SID,
        created_at=T0,
        last_updated=T0 + timedelta(days=1, hours=1),
        project_path="/repo",
        messages=[
            _msg(Role.USER, "first real prompt", 0),
            _msg(Role.ASSISTANT, "[Tool: Bash]", 1, tool_calls=[call]),
            _msg(Role.USER, "[Tool Result]\n1 failed", 2),
            _msg(Role.ASSISTANT, "The test fails.", 3),
            # Next day: the page must show a day separator.
            _msg(Role.USER, "second real prompt", 60 * 24 + 5),
            _msg(Role.ASSISTANT, "Fixed.", 60 * 24 + 6),
        ],
    )


@pytest.fixture()
def client(monkeypatch, tmp_path):
    session = _session()
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [session])
    index_payload = {
        "sessions": [
            {
                "id": SID,
                "tool": "claude-code",
                "title": "v2 view",
                "created": T0.isoformat(),
                "updated": (T0 + timedelta(days=1)).isoformat(),
                "project": "/repo",
                # The export and the source are gone: only the store has the body.
                "export_path": str(tmp_path / "projects" / "gone.md"),
                "messages": len(session.messages),
                "prompts": 3,
            }
        ],
        "stats": {},
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", lambda: index_payload)
    calls = []

    def _no_source(*args, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(web, "load_session_by_id", _no_source)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [])
    with web.app.test_client() as test_client:
        test_client.source_calls = calls
        yield test_client


def test_page_renders_all_prompts_from_the_store_without_export_or_source(client):
    page = client.get(f"/session/{SID}").get_data(as_text=True)
    assert "first real prompt" in page and "second real prompt" in page
    # The tool result joins the agent turn: two prompts, not three.
    assert page.count('class="toc-item"') == 2
    assert "2026-11-07 19:58" in page
    assert 'class="day-separator"' in page and "2026-11-08" in page
    assert "pytest -q" in page
    assert client.source_calls == []  # no tool source was touched


def test_store_build_is_cached_on_the_store_stamp(client, monkeypatch):
    client.get(f"/session/{SID}")
    builds = []
    real = sessions_routes._build_session_from_v2

    def _counting(*args, **kwargs):
        builds.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(sessions_routes, "_build_session_from_v2", _counting)
    client.get(f"/session/{SID}")
    client.get(f"/session/{SID}/messages?page=1")
    assert builds == []


def test_live_with_known_tool_does_not_try_other_tools(client):
    resp = client.get(f"/session/{SID}?live=1")
    assert resp.status_code == 200
    assert client.source_calls, "live view asks the tool's own source"
    assert all(not call.get("allow_cross_tool_fallback") for call in client.source_calls)


def test_summary_fallback_is_headed_by_the_session_title_not_rules(monkeypatch, tmp_path):
    entry = {
        "id": "sess-no-body",
        "tool": "claude-code",
        "title": "Fix the <b>parser</b>",
        "display_title": "Fix the <b>parser</b>",
        "created": T0.isoformat(),
        "updated": T0.isoformat(),
        "messages": 0,
        "prompts": 0,
    }
    payload = {"sessions": [entry], "stats": {}}
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web, "load_index", lambda: payload)
    monkeypatch.setattr(web, "load_session_by_id", lambda *a, **k: None)
    with web.app.test_client() as test_client:
        page = test_client.get("/session/sess-no-body").get_data(as_text=True)
    assert ">Rules</h1>" not in page
    assert "project-level instruction files" not in page
    assert "Fix the &lt;b&gt;parser&lt;/b&gt;</h1>" in page
    assert page.count("Fix the &lt;b&gt;parser&lt;/b&gt;</h") == 1  # no duplicate <h2>
