"""Turn-shaped Markdown export matches the web session view grouping."""

from __future__ import annotations

from datetime import datetime, timedelta

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.markdown import MarkdownExporter


def _session(messages, **kwargs) -> UnifiedSession:
    start = datetime(2026, 11, 9, 10, 0, 0)
    return UnifiedSession(
        tool=kwargs.get("tool", Tool.CLAUDE_CODE),
        session_id="turn-export",
        created_at=start,
        last_updated=start + timedelta(hours=2),
        title="Turn export",
        messages=messages,
        **{k: v for k, v in kwargs.items() if k != "tool"},
    )


def _render(session: UnifiedSession) -> str:
    from pathlib import Path

    return MarkdownExporter(Path("."))._generate_markdown(session)


def test_turn_export_full_flow():
    t0 = datetime(2026, 11, 9, 10, 0, 0)
    t1 = datetime(2026, 11, 10, 9, 0, 0)
    long_output = "\n".join(f"line {i}" for i in range(15))
    messages = [
        UnifiedMessage(role=Role.USER, content="Do the thing", timestamp=t0),
        UnifiedMessage(
            role=Role.ASSISTANT,
            content="",
            timestamp=t0 + timedelta(minutes=1),
            tool_calls=[
                {
                    "name": "Bash",
                    "input": {"command": "pytest -q"},
                    "output": long_output,
                    "status": "error",
                    "is_error": True,
                }
            ],
        ),
        UnifiedMessage(
            role=Role.ASSISTANT, content="All done.", timestamp=t0 + timedelta(minutes=2)
        ),
        UnifiedMessage(
            role=Role.USER,
            content="Base directory for this skill: /path/to/my-skill\n\n(rest)",
            timestamp=t0 + timedelta(minutes=3),
        ),
        UnifiedMessage(
            role=Role.USER,
            content="<command-name>foo</command-name><command-args>bar baz</command-args>",
            timestamp=t1,
        ),
    ]
    md = _render(_session(messages))
    assert md.count("\n### Prompt ") == 2
    assert "\n## Prompt" not in md  # Conversation (H2) > Prompt n (H3)
    assert "### Prompt 1 · 2026-11-09 10:00" in md
    assert "### Prompt 2 · 2026-11-10 09:00" in md
    assert "**Bash**" in md and "pytest -q" in md
    assert "✗" in md or "exit" in md.lower()
    assert "Output (15 lines)" in md
    assert "Skill loaded: my-skill" in md
    assert "/foo bar baz" in md
    assert "November" in md or "Monday" in md or "Sunday" in md  # day heading on date change
    assert "tool: claude-code" in md
    assert "session_id:" in md


def test_tool_events_only_produce_tool_lines():
    t0 = datetime(2026, 11, 9, 10, 0, 0)
    messages = [
        UnifiedMessage(role=Role.USER, content="go", timestamp=t0),
        UnifiedMessage(
            role=Role.ASSISTANT,
            content="",
            timestamp=t0,
            tool_events=[{"name": "Read", "status": "completed", "is_error": False}],
        ),
    ]
    md = _render(_session(messages))
    assert "**Read**" in md


def test_html_in_prompt_is_escaped():
    t0 = datetime(2026, 11, 9, 10, 0, 0)
    messages = [UnifiedMessage(role=Role.USER, content="<script>x</script>", timestamp=t0)]
    md = _render(_session(messages))
    assert "<script>" not in md
    assert "&lt;script&gt;" in md
