"""Tool-group folding in format_tool_calls and merged view controls (B5.5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lore.interfaces import web
from lore.interfaces.session_view_prep import render_message_body
from lore.interfaces.web_formatting import format_message_content, format_tool_calls


def _call(tool: str, **kwargs):
    base = {"tool": tool, "input": kwargs.get("input", {}), "output": kwargs.get("output", "")}
    for key in ("status", "duration_ms", "is_error"):
        if key in kwargs:
            base[key] = kwargs[key]
    if "_burst_count" in kwargs:
        base["_burst_count"] = kwargs["_burst_count"]
    return base


def test_three_calls_produce_one_tool_group_with_summary():
    calls = [
        _call("read", input={"path": "/a"}, status="ok", duration_ms=100),
        _call("read", input={"path": "/b"}, status="ok", duration_ms=200),
        _call("bash", input={"command": "ls"}, status="ok", duration_ms=300),
    ]
    html = format_tool_calls(calls)
    assert html.count('class="tool-group"') == 1
    assert "3 tool calls" in html
    assert "Read×2" in html or "Read&#215;2" in html
    assert "Bash" in html


def test_single_call_stays_plain_card_no_tool_group():
    html = format_tool_calls([_call("read", input={"path": "/x"})])
    assert "tool-group" not in html
    assert "action-block" in html


def test_failed_call_makes_group_open_and_shows_failed():
    calls = [
        _call("bash", status="ok", duration_ms=10),
        _call("bash", status="failed", is_error=True, duration_ms=20),
    ]
    html = format_tool_calls(calls)
    assert 'class="tool-group"' in html
    assert re.search(r"<details[^>]*\sopen", html)
    assert "failed" in html.lower()


def test_all_ok_group_closed_and_shows_all_ok():
    calls = [
        _call("read", status="ok", duration_ms=10),
        _call("edit", status="ok", duration_ms=20),
    ]
    html = format_tool_calls(calls)
    group = (
        html.split('class="tool-group"', 1)[0]
        + 'class="tool-group"'
        + html.split('class="tool-group"', 1)[1].split("</details>", 1)[0]
    )
    assert re.search(r"<details[^>]*class=\"tool-group\"", group)
    assert " open" not in group.split(">", 1)[0]
    assert "all ok" in html.lower()


def test_more_than_five_tool_names_shows_plus_k_more():
    calls = [_call(name, status="ok") for name in ("a", "b", "c", "d", "e", "f")]
    html = format_tool_calls(calls)
    assert "+1 more" in html or "+ 1 more" in html.replace(" ", "")


def test_duration_sum_only_when_all_known():
    partial = format_tool_calls(
        [
            _call("read", status="ok", duration_ms=100),
            _call("read", status="ok"),
        ]
    )
    assert "200ms" not in partial and "0.2s" not in partial
    full = format_tool_calls(
        [
            _call("read", status="ok", duration_ms=100),
            _call("read", status="ok", duration_ms=200),
        ]
    )
    assert "300ms" in full


def test_burst_count_included_in_tool_call_total():
    calls = [
        _call("read", _burst_count=2, status="ok", duration_ms=10),
        _call("bash", status="ok", duration_ms=10),
    ]
    html = format_tool_calls(calls)
    assert "3 tool calls" in html


def test_separate_format_runs_do_not_merge_across_prose():
    first = format_tool_calls([_call("read"), _call("read")])
    second = format_tool_calls([_call("bash"), _call("bash")])
    combined = first + "<p>prose</p>" + second
    assert combined.count('class="tool-group"') == 2


def test_tool_name_with_script_is_escaped_in_group_summary():
    calls = [
        _call("<script>alert(1)</script>", status="ok"),
        _call("read", status="ok"),
    ]
    html = format_tool_calls(calls)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_two_failed_shows_count_failed():
    calls = [
        _call("bash", status="failed", is_error=True),
        _call("bash", status="failed", is_error=True),
    ]
    html = format_tool_calls(calls)
    assert re.search(r"2\s+failed", html, re.IGNORECASE)


@pytest.fixture()
def detail_client(monkeypatch):
    from datetime import datetime

    from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
    from lore.interfaces.web_services import enrich_session_for_detail
    from lore.utils.text_processing import format_thinking

    sid = "sess-b55-view"
    entry = {
        "sessions": [
            {
                "id": sid,
                "tool": "claude-code",
                "title": "View controls",
                "display_title": "View controls",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo",
                "messages": 1,
                "prompts": 1,
                "tokens": 10,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 1,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo": 1},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo",
        title="View controls",
        messages=[
            UnifiedMessage(Role.USER, "hi", datetime(2026, 4, 1, 10, 0, 0)),
        ],
    )
    enrich_session_for_detail(
        session,
        entry["sessions"][0],
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    monkeypatch.setattr(web, "load_index", lambda: entry)
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, []),
    )
    return web.app.test_client()


def test_detail_page_has_merged_view_controls(detail_client):
    html = detail_client.get("/session/sess-b55-view?live=1").get_data(as_text=True)
    assert html.count('role="group"') >= 3
    assert 'aria-pressed="true"' in html
    assert 'data-action="set-view-tools"' in html or "set-view-tools" in html


def test_old_view_toggle_ids_removed_from_templates():
    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    session = Path("lore/templates/session.html").read_text(encoding="utf-8")
    for old_id in (
        "cleanToggle",
        "ultraCleanToggle",
        "readabilityToggle",
        "presenterToggle",
        "densityToggle",
    ):
        assert old_id not in base
        assert old_id not in session


def test_view_migration_script_references_legacy_keys():
    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert "lore.view.v2" in base
    for legacy in (
        "aihistory-density",
        "aihistory-clean-mode",
        "aihistory-ultra-clean",
        "aihistory-readability",
        "aihistory-presenter-mode",
    ):
        assert legacy in base


def test_body_default_view_data_attributes(detail_client):
    html = detail_client.get("/session/sess-b55-view?live=1").get_data(as_text=True)
    body_match = re.search(r"<body[^>]*>", html)
    assert body_match
    tag = body_match.group(0)
    assert 'data-tools="fold"' in tag
    assert 'data-thinking="fold"' in tag
    assert 'data-density="comfortable"' in tag


def test_render_message_body_groups_consecutive_tools():
    from datetime import datetime

    from lore.core.models import Role, UnifiedMessage

    msg = UnifiedMessage(
        Role.ASSISTANT,
        "intro",
        datetime(2026, 1, 1),
        tool_calls=[_call("read"), _call("read"), _call("bash")],
    )
    body = render_message_body(
        msg,
        format_message_content_fn=format_message_content,
        format_tool_calls_fn=format_tool_calls,
    )
    assert body.count('class="tool-group"') == 1
    assert "intro" in body or "<p>" in body


def test_group_without_confirmed_success_is_not_labelled_all_ok():
    from lore.interfaces.web_formatting import format_tool_calls

    calls = [{"tool": "Bash", "input": {"cmd": "ls"}}, {"tool": "Read", "input": {"path": "/a"}}]

    html = format_tool_calls(calls)

    assert "all ok" not in html
    assert "status unknown" in html


def test_error_flag_on_the_call_counts_even_when_the_event_has_none():
    from lore.interfaces.web_formatting import format_tool_calls

    calls = [
        {"tool": "Bash", "input": {}, "is_error": True},
        {"tool": "Read", "input": {}, "status": "ok"},
    ]
    events = [
        {"name": "Bash", "status": "completed"},
        {"name": "Read", "status": "completed"},
    ]

    html = format_tool_calls(calls, events)

    assert "1 failed" in html
    assert 'data-default-open="1"' in html


def test_view_state_javascript_validates_stored_values_and_does_not_promote_open_groups():
    from pathlib import Path

    source = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert "['hide', 'fold', 'open']" in source
    assert "['comfortable', 'compact']" in source
    assert "setAttribute('data-default-open', '1')" not in source
