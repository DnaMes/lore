from lore.interfaces import web
from lore.interfaces.web_formatting import format_message_content, format_tool_calls
from lore.utils.text_processing import (
    format_duration_ms,
    format_tool_display,
    format_tool_result,
)


def test_format_tool_display_renders_command_and_raw_args():
    html = format_tool_display("bash", '{"command":"ls -la","cwd":"/tmp"}')

    assert "Run bash" in html
    assert "Command" in html
    assert "language-bash" in html
    assert "Raw arguments" in html
    assert "cwd" in html


def test_format_tool_result_marks_error_status_and_metadata():
    html = format_tool_result("Traceback: failed to run command")

    assert "status-error" in html
    assert "Tool result" in html
    assert "lines" in html
    assert "chars" not in html


def test_format_duration_ms_cases():
    assert format_duration_ms(None) is None
    assert format_duration_ms(0) == "0ms"
    assert format_duration_ms(340) == "340ms"
    assert format_duration_ms(1200) == "1.2s"
    assert format_duration_ms(65_000) == "65s"
    assert format_duration_ms(-1) is None
    assert format_duration_ms("fast") is None


def test_format_tool_display_and_result_never_emit_chars_chip():
    display = format_tool_display("read", '{"path":"/tmp/x"}', status="ok", duration_ms=200)
    result = format_tool_result("output", status="ok", duration_ms=200)
    assert "chars" not in display.lower()
    assert "chars" not in result.lower()


def test_format_tool_result_includes_exit_warning_and_diff_stats():
    html = format_tool_result("warning: foo\nexit code: 2\n+added\n-removed")

    assert "exit 2" in html
    assert "warnings 1" in html
    assert "diff +1/-1" in html
    assert "importance-major" in html


def test_format_message_content_supports_tool_names_with_symbols():
    rendered = web.format_message_content(
        '[Tool: web-fetch.v2] {"url":"https://example.com"}\n[Tool Result]\nOK'
    )

    assert "Run web-fetch.v2" in rendered
    assert "Tool result" in rendered


def test_format_tool_calls_renders_output_blocks_when_present():
    rendered = web.format_tool_calls(
        [
            {
                "name": "bash",
                "input": {"command": "echo hello"},
                "output": "hello",
            }
        ]
    )

    assert "Run bash" in rendered
    assert "Tool result" in rendered
    assert "echo hello" in rendered


def test_format_tool_display_marks_major_and_minor_actions():
    major = format_tool_display("bash", '{"command":"echo hi"}')
    minor = format_tool_display("read", '{"path":"/tmp/a.txt"}')

    assert "importance-major" in major
    assert "importance-minor" in minor


def test_format_tool_display_wraps_long_bash_commands_for_readability():
    html = format_tool_display(
        "bash",
        '{"command":"ls /one/two/three/four/five/six/seven/eight/nine/ten/eleven/twelve && cat /tmp/data.json | jq .items[]"}',
    )

    assert "&amp;&amp;\n" in html
    assert "|\njq" in html
    assert "Raw arguments" in html


def test_format_message_content_empty_returns_empty():
    assert format_message_content("") == ""
    assert format_message_content(None) == ""


def test_format_message_content_with_thinking_tags():
    result = format_message_content("<thinking>internal thought</thinking> After")
    assert "internal thought" in result or "After" in result


def test_format_message_content_with_command_message_tag():
    result = format_message_content("<command-message>run ls</command-message>")
    assert "run ls" in result


def test_format_message_content_with_command_name_tag():
    result = format_message_content("<command-name>git commit</command-name>")
    assert "git commit" in result


def test_format_message_content_with_command_args_tag():
    result = format_message_content("<command-args>-m 'msg'</command-args>")
    assert "-m" in result


def test_format_tool_calls_empty_returns_empty():
    assert format_tool_calls([]) == ""
    assert format_tool_calls(None) == ""


def test_format_tool_calls_string_input():
    # input is a plain string (not dict/list), triggers else branch
    result = format_tool_calls([{"name": "bash", "input": "echo hello"}])
    assert "Run bash" in result


def test_render_base_includes_clean_mode_toggle(monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": [],
            "stats": {
                "total_sessions": 0,
                "total_messages": 0,
                "by_tool": {},
                "by_project": {},
            },
        },
    )
    page = web.render(
        "dashboard",
        active="dashboard",
        stats={
            "total_sessions": 0,
            "total_messages": 0,
            "by_tool": {},
            "by_project": {},
        },
        recent_sessions=[],
        all_tools=[],
        title="Dashboard",
    )

    # Formatting buttons should NOT be present on dashboard (only in session view)
    assert 'id="cleanToggle"' not in page
    assert 'id="readabilityToggle"' not in page
    assert 'id="ultraCleanToggle"' not in page
    assert 'id="presenterToggle"' not in page
    assert 'id="densityToggle"' not in page
    # But these controls should always be present
    assert 'id="syncBtn"' in page
    assert 'id="cancelActionBtn"' in page
    assert "initReloadProviderScope()" in page
    assert "getSelectedProvider()" in page
    assert "cancelActiveAction()" in page


def test_render_session_includes_formatting_toggles(monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": [],
            "stats": {
                "total_sessions": 0,
                "total_messages": 0,
                "by_tool": {},
                "by_project": {},
            },
        },
    )
    page = web.render(
        "session",
        active="sessions",
        session=type(
            "Session",
            (),
            {
                "session_id": "test-123",
                "title": "Test Session",
                "tool": type("Tool", (), {"value": "claude-code"})(),
                "created_at": __import__("datetime").datetime.now(),
                "project_path": None,
                "prompt_count": 1,
                "pairs": [],
                "visible_count": 0,
                "thread_id": None,
            },
        )(),
        style=web.get_style("claude-code"),
        toc_items=[],
        back_target="/sessions",
        title="Test Session",
    )

    assert 'data-action="set-view-tools"' in page
    assert 'data-action="set-view-thinking"' in page
    assert 'data-action="set-view-density"' in page
    assert "lore.view.v2" in page
    assert 'data-tools="fold"' in page
