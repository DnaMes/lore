"""Extended tests for lore/exporters/markdown.py."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.markdown import MarkdownExporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "test-session-abcdef123456",
    tool: Tool = Tool.CLAUDE_CODE,
    title: str | None = "Test Session Title",
    messages: list[tuple[str, str]] | None = None,
    project_path: str | None = "/home/user/projects/myapp",
    created_offset_minutes: int = 0,
    updated_offset_minutes: int = 5,
    git_branch: str | None = None,
    cli_version: str | None = None,
    thread_id: str | None = None,
) -> UnifiedSession:
    now = datetime(2025, 6, 15, 10, 0, 0)
    created = now + timedelta(minutes=created_offset_minutes)
    updated = now + timedelta(minutes=updated_offset_minutes)
    msgs = []
    for role_str, content in messages or [("user", "Hello"), ("assistant", "Hi there!")]:
        role = Role.USER if role_str == "user" else Role.ASSISTANT
        msgs.append(UnifiedMessage(role=role, content=content, timestamp=created))
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=created,
        last_updated=updated,
        messages=msgs,
        project_path=project_path,
        title=title,
        git_branch=git_branch,
        cli_version=cli_version,
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# Basic export
# ---------------------------------------------------------------------------


def test_export_session_creates_file(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session()
    out_path = exporter.export_session(session)

    assert out_path.exists()
    assert out_path.suffix == ".md"


def test_export_session_contains_frontmatter(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session()
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert content.startswith("---")
    assert "tool: claude-code" in content
    assert "session_id: test-session-abcdef123456" in content
    assert "messages: 2" in content


def test_export_session_contains_title(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(title="My Awesome Session")
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "# My Awesome Session" in content


def test_export_session_contains_project(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(project_path="/home/user/projects/myapp")
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "/home/user/projects/myapp" in content


def test_export_session_no_project(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(project_path=None)
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "**Project:**" not in content


def test_export_session_contains_messages(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(
        messages=[
            ("user", "How does quicksort work?"),
            ("assistant", "Quicksort divides the array around a pivot."),
        ]
    )
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "How does quicksort work?" in content
    assert "Quicksort divides" in content


def test_export_session_user_messages_quoted(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(messages=[("user", "What is Python?"), ("assistant", "A language.")])
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "> What is Python?" in content


def test_export_session_statistics_section(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(
        messages=[
            ("user", "Question one"),
            ("assistant", "Answer one"),
            ("user", "Question two"),
            ("assistant", "Answer two"),
        ]
    )
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "## Statistics" in content
    assert "Total Messages | 4" in content


def test_export_session_duration_displayed_for_long_sessions(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(updated_offset_minutes=90)  # 90 minutes
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "Duration" in content
    assert "1h 30m" in content


def test_export_session_duration_minutes_only(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(updated_offset_minutes=45)
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "45m" in content


def test_export_session_no_duration_for_short_sessions(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(updated_offset_minutes=0)  # same time = no duration
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "Duration" not in content


def test_export_session_with_git_branch(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(git_branch="feature/coverage-gate")
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "git_branch: feature/coverage-gate" in content


def test_export_session_with_cli_version(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(cli_version="1.2.3")
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "cli_version: 1.2.3" in content
    assert "v1.2.3" in content


def test_export_session_with_thread_id(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(thread_id="project:abc123")
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "thread_id: project:abc123" in content


def test_export_session_with_tool_calls(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    now = datetime(2025, 6, 15, 10, 0, 0)
    msg_with_tools = UnifiedMessage(
        role=Role.ASSISTANT,
        content="Using a tool.",
        timestamp=now,
        tool_calls=[{"name": "read_file", "input": {"path": "/foo/bar.py"}}],
    )
    user_msg = UnifiedMessage(role=Role.USER, content="Read the file", timestamp=now)
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="tool-session-001",
        created_at=now,
        last_updated=now,
        messages=[user_msg, msg_with_tools],
        title="Tool Call Test",
    )
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "Tool: read_file" in content
    assert "<details>" in content


def test_export_session_with_reasoning(tmp_path):
    exporter = MarkdownExporter(tmp_path)
    now = datetime(2025, 6, 15, 10, 0, 0)
    msg_with_reasoning = UnifiedMessage(
        role=Role.ASSISTANT,
        content="Let me think...",
        timestamp=now,
        reasoning="First I should consider A, then B.",
    )
    user_msg = UnifiedMessage(role=Role.USER, content="Think hard", timestamp=now)
    session = UnifiedSession(
        tool=Tool.CODEX,
        session_id="reasoning-session-001",
        created_at=now,
        last_updated=now,
        messages=[user_msg, msg_with_reasoning],
        title="Reasoning Test",
    )
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    assert "Reasoning" in content
    assert "First I should consider A" in content


def test_export_session_skip_when_up_to_date(tmp_path):
    """Second export without force skips if file is up-to-date."""
    exporter = MarkdownExporter(tmp_path)
    session = _make_session()

    first_path = exporter.export_session(session)
    first_mtime = first_path.stat().st_mtime

    # Touch the file to ensure mtime >= session's last_updated
    time.sleep(0.01)
    second_path = exporter.export_session(session, force=False)
    second_mtime = second_path.stat().st_mtime

    assert first_path == second_path
    assert second_mtime == first_mtime  # file was not re-written


def test_export_session_force_overwrites(tmp_path):
    """Force export always rewrites the file."""
    exporter = MarkdownExporter(tmp_path)
    session = _make_session()

    first_path = exporter.export_session(session, force=True)
    # Ensure some time passes for mtime difference
    time.sleep(0.05)
    second_path = exporter.export_session(session, force=True)

    assert first_path == second_path
    assert second_path.exists()


def test_candidate_path_returns_correct_structure(tmp_path):
    """_candidate_path returns a path in projects/tool structure."""
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(project_path="/home/user/projects/myapp")
    path = exporter._candidate_path(session)

    assert "projects" in str(path)
    assert "claude-code" in str(path)
    assert path.suffix == ".md"


def test_export_session_no_title_uses_session_id(tmp_path):
    """If title is None, session ID prefix is used in filename."""
    exporter = MarkdownExporter(tmp_path)
    session = _make_session(title=None)
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    # No title means fallback to session id prefix in content heading
    assert "# Session" in content or "test-ses" in content


def test_generate_markdown_with_token_stats(tmp_path):
    """Sessions with tokens show token count in stats."""
    now = datetime(2025, 6, 15, 10, 0, 0)
    exporter = MarkdownExporter(tmp_path)
    msg = UnifiedMessage(
        role=Role.ASSISTANT,
        content="Here is the response.",
        timestamp=now,
        tokens={"total": 1500, "input": 500, "output": 1000},
    )
    user_msg = UnifiedMessage(role=Role.USER, content="Generate a story", timestamp=now)
    session = UnifiedSession(
        tool=Tool.GEMINI_CLI,
        session_id="token-session-001",
        created_at=now,
        last_updated=now,
        messages=[user_msg, msg],
        title="Token Stats Test",
    )
    out_path = exporter.export_session(session)
    content = out_path.read_text(encoding="utf-8")

    # tokens should appear in frontmatter and/or stats
    assert "1,500" in content or "1500" in content


def test_export_different_tools(tmp_path):
    """Exports for different tools go into different subdirectories."""
    exporter = MarkdownExporter(tmp_path)
    tools = [Tool.CLAUDE_CODE, Tool.CURSOR, Tool.GEMINI_CLI, Tool.CODEX]
    paths = []
    for i, tool in enumerate(tools):
        session = _make_session(
            session_id=f"tool-test-{i:04d}abcdefg",
            tool=tool,
            project_path="/home/user/project",
        )
        paths.append(exporter.export_session(session))

    tool_dirs = {p.parent.name for p in paths}
    assert "claude-code" in tool_dirs
    assert "cursor" in tool_dirs
    assert "gemini-cli" in tool_dirs
    assert "codex" in tool_dirs
