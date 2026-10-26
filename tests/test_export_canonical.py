"""Canonical tool calls and complete transcripts in both exporters (#166, #165).

The Markdown and HTML exports used to render only ``tool_calls`` with a name and
an ``input`` blob, so a call's output, error state, identity and truncation were
lost, and the compact ``tool_events`` the v2 store keeps were ignored entirely —
which is where "Unknown Tool" and the missing results came from.

These tests pin the canonical envelope (``lore/exporters/tool_calls.py``) and the
completeness of the transcript: every message in order (including empty, system
and unmatched tool messages), exact code whitespace, and derived metadata that is
labelled instead of replacing transcript content.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lore.core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.html import render_session_html
from lore.exporters.markdown import MarkdownExporter
from lore.exporters.tool_calls import canonical_tool_calls, tool_call_markdown

START = datetime(2026, 3, 1, 10, 0, 0)


def _message(role: Role, content: str, minutes: int = 0, **kwargs) -> UnifiedMessage:
    return UnifiedMessage(
        role=role, content=content, timestamp=START + timedelta(minutes=minutes), **kwargs
    )


@pytest.fixture()
def structured_session() -> UnifiedSession:
    """One session that exercises every shape the envelope has to handle."""
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="ses_export",
        created_at=START,
        last_updated=START + timedelta(minutes=10),
        project_path="/repo/lore",
        title="Export fixture",
        title_source=TitleSource.LLM,
        summary="A generated summary that must not replace the transcript.",
        messages=[
            _message(Role.USER, "list the files", 0),
            _message(
                Role.ASSISTANT,
                "",
                1,
                tool_calls=[
                    {
                        "id": "call_ok",
                        "name": "Bash",
                        "input": {"command": "ls -la", "timeout": 5},
                        "output": "total 0\ndrwxr-xr-x 2 user user 40 file.txt",
                        "status": "completed",
                        "duration_ms": 1200,
                    },
                    {
                        "id": "call_bad",
                        "name": "Read",
                        "input": "not-json{",
                        "output": "ENOENT: no such file",
                        "status": "error",
                        "is_error": True,
                        "truncated": True,
                    },
                ],
                tool_events=[
                    {"name": "Bash", "status": "completed", "duration_ms": 1200, "is_error": False},
                    {"name": "Read", "status": "error", "is_error": True},
                ],
            ),
            _message(Role.TOOL, "orphan result without a pairing", 2),
            _message(Role.SYSTEM, "system note", 3),
            _message(Role.ASSISTANT, "```python\nprint('a')  \nprint('b')\n```", 4),
        ],
    )


def _markdown(session: UnifiedSession) -> str:
    return MarkdownExporter(Path("."))._generate_markdown(session)


# ---------------------------------------------------------------------------
# canonical envelope (#166)
# ---------------------------------------------------------------------------


def test_envelope_reads_name_arguments_output_and_state(structured_session):
    calls = canonical_tool_calls(structured_session.messages[1])
    assert [call.name for call in calls] == ["Bash", "Read"]
    first, second = calls
    assert first.call_id == "call_ok"
    assert '"command": "ls -la"' in first.arguments
    assert first.arguments_is_json is True
    assert first.output.startswith("total 0")
    assert first.state_label == "completed"
    assert first.duration_ms == 1200
    assert second.state_label == "failed"
    assert second.arguments_is_json is False
    assert second.arguments == "not-json{"
    assert second.truncated is True


def test_envelope_does_not_duplicate_tool_events(structured_session):
    """The matching events must not be rendered a second time as bare calls."""
    calls = canonical_tool_calls(structured_session.messages[1])
    assert len(calls) == 2


def test_envelope_keeps_an_event_without_a_call():
    message = _message(
        Role.ASSISTANT,
        "",
        tool_calls=[],
        tool_events=[{"name": "Edit", "status": "error", "is_error": True}],
    )
    calls = canonical_tool_calls(message)
    assert [call.name for call in calls] == ["Edit"]
    assert calls[0].state_label == "failed"


def test_unmatched_result_is_visible():
    message = _message(Role.TOOL, "orphan", 0)
    calls = canonical_tool_calls(message)
    assert calls and calls[0].unmatched_result is True
    assert calls[0].output == "orphan"


def test_markdown_shows_every_field_of_a_call(structured_session):
    markdown = _markdown(structured_session)
    assert "<summary>Tool: Bash — completed (1200 ms)</summary>" in markdown
    assert "**Call id:** `call_ok`" in markdown
    assert '"command": "ls -la"' in markdown
    assert "**Result:**" in markdown
    assert "total 0" in markdown
    assert "**Error:** (truncated)" in markdown
    assert "(not valid JSON)" in markdown


def test_markdown_and_html_agree_on_the_call_set(structured_session):
    markdown = _markdown(structured_session)
    page = render_session_html(structured_session)
    for name in ("Bash", "Read"):
        assert f"Tool: {name}" in markdown or f"Tool: {name} —" in markdown
        assert f"Tool: {name}" in page
    assert "orphan result without a pairing" in markdown
    assert "orphan result without a pairing" in page


def test_markdown_tool_block_is_deterministic(structured_session):
    first = _markdown(structured_session)
    second = _markdown(structured_session)
    assert first == second


def test_markdown_keeps_a_large_output_verbatim():
    big = "\n".join(f"line {index}" for index in range(500))
    message = _message(Role.ASSISTANT, "", tool_calls=[{"id": "c1", "name": "Bash", "output": big}])
    rendered = "\n".join(tool_call_markdown(canonical_tool_calls(message)[0]))
    assert big in rendered


# ---------------------------------------------------------------------------
# transcript completeness (#165)
# ---------------------------------------------------------------------------


def test_markdown_contains_every_message_in_order(structured_session):
    markdown = _markdown(structured_session)
    markers = [
        "list the files",
        "Tool: Bash",
        "Tool: Read",
        "orphan result without a pairing",
        "### System",
        "system note",
        "print('a')",
    ]
    positions = [markdown.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_markdown_keeps_exact_code_whitespace(structured_session):
    markdown = _markdown(structured_session)
    assert "print('a')  \nprint('b')" in markdown


def test_markdown_marks_derived_metadata(structured_session):
    markdown = _markdown(structured_session)
    assert "*Title source: llm (derived, not transcript content).*" in markdown
    assert "**Summary (derived, not transcript content):**" in markdown
    assert "title_source: llm" in markdown


def test_html_marks_derived_metadata(structured_session):
    page = render_session_html(structured_session)
    assert "Title source: llm (derived)" in page
    assert "Summary (derived, not transcript content):" in page
    assert "system note" in page


def test_empty_session_is_explicit_in_both_formats():
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="ses_empty",
        created_at=START,
        last_updated=START,
        project_path="/repo/lore",
        messages=[],
    )
    assert "_No messages were captured for this session._" in _markdown(session)
    assert "No messages were captured for this session." in render_session_html(session)


def test_exports_work_with_every_provider_unavailable(monkeypatch, structured_session):
    """An export must never need a cloud or local model to succeed."""
    import lore.llm.factory as factory

    def explode(*_args, **_kwargs):
        raise AssertionError("an export must not construct an LLM provider")

    for name in [value for value in dir(factory) if not value.startswith("_")]:
        candidate = getattr(factory, name)
        if callable(candidate) and "provider" in name.lower():
            monkeypatch.setattr(factory, name, explode, raising=False)
    assert "list the files" in _markdown(structured_session)
    assert "list the files" in render_session_html(structured_session)


def test_html_escapes_tool_content():
    message = _message(
        Role.ASSISTANT,
        "",
        tool_calls=[{"id": "c1", "name": "Bash<script>", "output": "<img src=x onerror=alert(1)>"}],
    )
    page = render_session_html(
        UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="ses_x",
            created_at=START,
            last_updated=START,
            messages=[message],
        )
    )
    assert "<script>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
