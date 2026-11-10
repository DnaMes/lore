"""Session page readability, phase 1: turn cards, tool rows, output preview, dates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces.web_data import build_session_from_export_markdown
from lore.interfaces.web_formatting import format_message_content, format_tool_calls
from lore.interfaces.web_services import annotate_turns, enrich_session_for_detail
from lore.utils.text_processing import format_thinking, format_tool_result, tool_call_target

T0 = datetime(2026, 11, 7, 23, 50, tzinfo=timezone.utc)


def _msg(role: Role, content: str, minutes: int = 0, **kwargs) -> UnifiedMessage:
    return UnifiedMessage(
        role=role, content=content, timestamp=T0 + timedelta(minutes=minutes), **kwargs
    )


def _enrich(*messages: UnifiedMessage) -> tuple[UnifiedSession, list]:
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="s1",
        created_at=T0,
        last_updated=T0,
        messages=list(messages),
    )
    toc = enrich_session_for_detail(
        session, {}, format_message_content, format_tool_calls, format_thinking
    )
    return session, toc


# --- tool rows: target + status, escaped -------------------------------------------------


def test_collapsed_group_has_one_row_per_call_with_target_and_status():
    html = format_tool_calls(
        [
            {"tool": "Read", "input": {"file_path": "lore/x/web.py"}, "status": "ok"},
            {
                "tool": "Bash",
                "input": {"command": "pytest -q"},
                "status": "failed",
                "exit_code": 1,
            },
        ]
    )
    assert html.count('class="tool-row"') == 2
    assert '<span class="tool-row-target">lore/x/web.py</span>' in html
    assert '<span class="tool-row-target">pytest -q</span>' in html
    assert "✗</span> exit 1" in html
    assert 'data-status="ok"' in html


def test_tool_row_target_is_escaped():
    html = format_tool_calls(
        [
            {"tool": "Bash", "input": {"command": "<script>alert(1)</script>"}},
            {"tool": "Read", "input": {"file_path": "/a"}},
        ]
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_tool_call_target_reads_dicts_json_and_fenced_text():
    assert tool_call_target({"file_path": "/x.py", "limit": 5}) == "/x.py"
    assert tool_call_target('{"command": "ls -la"}') == "ls -la"
    assert tool_call_target("```bash\ngit status\n```") == "git status"
    assert tool_call_target({"unrelated": 1}) == ""


# --- long output folds to a preview -------------------------------------------------------


def test_long_tool_output_is_marked_for_a_ten_line_preview():
    long_html = format_tool_result("\n".join(f"line {i}" for i in range(40)))
    assert 'data-preview-lines="10"' in long_html
    assert "line 39" in long_html  # the full text stays in the DOM for copy
    short_html = format_tool_result("\n".join(f"line {i}" for i in range(12)))
    assert "data-preview-lines" not in short_html


# --- dates and day separators -------------------------------------------------------------


def test_day_label_only_where_the_date_changes():
    pairs = [
        {"user": _msg(Role.USER, "a", 0), "responses": []},
        {"user": _msg(Role.USER, "b", 5), "responses": []},
        {"user": _msg(Role.USER, "c", 20), "responses": []},  # past midnight
    ]
    annotate_turns(pairs)
    assert pairs[0]["day_label"] == "Saturday, 2026-11-07"
    assert pairs[1]["day_label"] == ""
    assert pairs[2]["day_label"] == "Sunday, 2026-11-08"


def test_export_parser_rolls_the_date_when_the_clock_runs_backwards(tmp_path: Path):
    export = tmp_path / "s.md"
    export.write_text(
        "# T\n\n## Conversation\n\n### User (23:50:00)\n\n> late\n\n---\n\n"
        "### Assistant (00:10:00)\n\nnext day\n",
        encoding="utf-8",
    )
    session = build_session_from_export_markdown(
        "s1", {"created": "2026-11-07T23:50:00+00:00", "tool": "claude-code"}, export
    )
    assert [m.timestamp.date().isoformat() for m in session.messages] == [
        "2026-11-07",
        "2026-11-08",
    ]


# --- tool results join their agent turn ---------------------------------------------------


def test_tool_result_user_record_joins_the_turn_not_the_outline():
    session, toc = _enrich(
        _msg(Role.USER, "real prompt"),
        _msg(Role.ASSISTANT, "[Tool: Bash]\n```bash\nls\n```", 1),
        _msg(Role.USER, "[Tool Result]\nfile.txt", 2),
        _msg(Role.ASSISTANT, "done", 3),
    )
    assert len(session.pairs) == 1
    assert [item["text"] for item in toc] == ["real prompt"]
    assert session.prompt_count == 1
    assert len(session.pairs[0]["responses"]) == 3


def test_flat_tool_calls_and_results_fold_into_one_group():
    session, _toc = _enrich(
        _msg(Role.USER, "go"),
        _msg(Role.ASSISTANT, "[Tool: Bash]\n```bash\nmake test\n```", 1),
        _msg(Role.USER, "[Tool Result]\nExit code 2\nboom", 2),
        _msg(Role.ASSISTANT, "[Tool: Read] /src/app.py", 3),
        _msg(Role.USER, "[Tool Result]\nprint('hi')", 4),
        _msg(Role.ASSISTANT, "Found it.", 5),
    )
    responses = session.pairs[0]["responses"]
    group = responses[0].formatted_content
    assert group.count('class="tool-row"') == 2
    assert "make test" in group and "/src/app.py" in group
    assert "✗</span> exit 2" in group
    assert all(r.merged_into_group for r in responses[1:4])
    assert not responses[4].merged_into_group
    assert session.pairs[0]["step_count"] == 2


def test_result_already_shown_by_a_structured_call_is_hidden():
    call = {"tool": "Bash", "input": {"command": "ls"}, "output": "file.txt"}
    session, _toc = _enrich(
        _msg(Role.USER, "go"),
        _msg(Role.ASSISTANT, "[Tool: Bash]", 1, tool_calls=[call]),
        _msg(Role.TOOL, "file.txt", 2),
    )
    responses = session.pairs[0]["responses"]
    assert responses[1].merged_into_group
    assert responses[0].formatted_content.count("file.txt") >= 1
