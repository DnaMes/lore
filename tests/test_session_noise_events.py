"""Injected transcript noise renders as folded in-turn events, not prompt cards (#10)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_utils
from lore.interfaces.routes import sessions as sessions_routes
from lore.interfaces.web_formatting import (
    _tool_group_row_html,
    format_message_content,
    format_tool_calls,
)
from lore.interfaces.web_services import enrich_session_for_detail
from lore.utils.text_processing import format_thinking

T0 = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def _msg(role: Role, content: str, minutes: int = 0) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0.replace(minute=minutes))


def _session(tool: Tool, *messages: UnifiedMessage) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id="noise-events",
        created_at=T0,
        last_updated=T0,
        messages=list(messages),
    )


def _enrich(session: UnifiedSession) -> tuple[UnifiedSession, list]:
    toc = enrich_session_for_detail(
        session,
        {},
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    return session, toc


def _render_session_html(session: UnifiedSession, monkeypatch, tmp_path) -> str:
    index_payload = {
        "sessions": [
            {
                "id": session.session_id,
                "tool": session.tool.value,
                "title": "noise",
                "created": T0.isoformat(),
                "updated": T0.isoformat(),
                "project": "/repo",
                "messages": len(session.messages),
                "prompts": session.prompt_count,
            }
        ],
        "stats": {},
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", lambda: index_payload)
    monkeypatch.setattr(web, "load_session_by_id", lambda *_a, **_k: session)
    with web.app.test_client() as client:
        resp = client.get(f"/session/{session.session_id}?live=1")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_skill_body_is_folded_event_not_prompt_or_toc():
    skill = (
        "Base directory for this skill: /home/x/.claude/skills/systematic-debugging\n\n"
        "# Skill body\n"
    )
    session, toc = _enrich(
        _session(
            Tool.CLAUDE_CODE,
            _msg(Role.USER, skill, 0),
            _msg(Role.ASSISTANT, "ok", 1),
        )
    )
    assert session.prompt_count == 0
    assert toc == []
    assert session.pairs[0]["user"] is None
    event = session.pairs[0]["responses"][0]
    assert event.display_role == "event"
    assert "Skill loaded: systematic-debugging" in event.formatted_content
    assert "Base directory for this skill" in event.formatted_content


def test_slash_command_becomes_prompt_titled_name_and_args():
    body = (
        "<command-message>run</command-message>"
        "<command-name>/review</command-name>"
        "<command-args>the diff please</command-args>"
    )
    session, toc = _enrich(
        _session(
            Tool.CLAUDE_CODE,
            _msg(Role.USER, body, 0),
            _msg(Role.ASSISTANT, "done", 1),
        )
    )
    assert session.prompt_count == 1
    assert toc[0]["text"].startswith("/review")
    assert "the diff please" in toc[0]["text"]
    assert session.pairs[0]["user"] is not None
    assert "/review" in session.pairs[0]["user"].formatted_content
    assert "<command-name>" not in session.pairs[0]["user"].formatted_content


def test_hook_feedback_task_and_system_reminder_are_events():
    session, toc = _enrich(
        _session(
            Tool.CLAUDE_CODE,
            _msg(Role.USER, "real question", 0),
            _msg(Role.ASSISTANT, "thinking", 1),
            _msg(Role.USER, "Stop hook feedback: blocked claim", 2),
            _msg(Role.USER, "<task-notification>done</task-notification>", 3),
            _msg(Role.USER, "<system-reminder>remember hooks</system-reminder>", 4),
            _msg(Role.ASSISTANT, "answer", 5),
        )
    )
    assert session.prompt_count == 1
    assert [item["text"] for item in toc] == ["real question"]
    labels = " ".join(
        r.formatted_content
        for r in session.pairs[0]["responses"]
        if getattr(r, "display_role", "") == "event"
    )
    assert "Hook feedback" in labels
    assert "Task notification" in labels
    assert "System reminder" in labels


def test_toolu_notification_echo_is_event():
    echo = "a1b2c3d4 toolu_01ABC run completed"
    session, toc = _enrich(
        _session(Tool.CLAUDE_CODE, _msg(Role.USER, echo), _msg(Role.ASSISTANT, "ok", 1))
    )
    assert session.prompt_count == 0
    assert toc == []
    assert "Task notification" in session.pairs[0]["responses"][0].formatted_content
    assert echo in session.pairs[0]["responses"][0].formatted_content


def test_local_command_tags_only_are_events():
    session, _toc = _enrich(
        _session(
            Tool.CLAUDE_CODE,
            _msg(Role.USER, "<local-command-caveat>generated locally</local-command-caveat>"),
        )
    )
    assert session.prompt_count == 0
    assert "Local command" in session.pairs[0]["responses"][0].formatted_content


def test_codex_injected_system_prompt_is_event():
    body = "<environment_context>\nOS: linux\n</environment_context>"
    session, toc = _enrich(
        _session(
            Tool.CODEX,
            _msg(Role.SYSTEM, body, 0),
            _msg(Role.USER, "Ship it", 1),
            _msg(Role.ASSISTANT, "On it", 2),
        )
    )
    assert session.prompt_count == 1
    assert toc[0]["text"] == "Ship it"
    assert any("System context" in r.formatted_content for r in session.pairs[0]["responses"])


def test_base_directory_mid_text_stays_prompt():
    text = "Please read the Base directory for this skill docs and fix the bug"
    session, toc = _enrich(
        _session(Tool.CLAUDE_CODE, _msg(Role.USER, text), _msg(Role.ASSISTANT, "ok", 1))
    )
    assert session.prompt_count == 1
    assert session.pairs[0]["user"] is not None
    assert toc[0]["text"] == text[:88]


def test_event_html_is_escaped(monkeypatch, tmp_path):
    raw = "<task-notification><script>alert(1)</script></task-notification>"
    session = _session(Tool.CLAUDE_CODE, _msg(Role.USER, raw), _msg(Role.ASSISTANT, "ok", 1))
    _enrich(session)
    html = _render_session_html(session, monkeypatch, tmp_path)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_data_status_none_is_empty_string():
    row = _tool_group_row_html("Bash", "ls", None, None, 1)
    assert 'data-status=""' in row
    assert 'data-status="None"' not in row


def test_noise_rules_version_memo_skips_reload_when_unchanged(monkeypatch, tmp_path):
    noise_path = tmp_path / "noise_rules.json"
    noise_path.write_text(json.dumps({"default": {"strip_ansi_sequences": True}}), encoding="utf-8")
    monkeypatch.setattr(web_utils, "NOISE_RULES_PATH", noise_path)
    sessions_routes._NOISE_RULES_VERSION_MEMO.clear()

    loads = []
    real_load = web_utils.load_noise_rules

    def _counting():
        loads.append(1)
        return real_load()

    monkeypatch.setattr(web, "load_noise_rules", _counting)

    first = sessions_routes._noise_rules_version()
    second = sessions_routes._noise_rules_version()
    assert first == second
    assert len(loads) == 1
