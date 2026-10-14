"""Markup contract for session detail: tools, thinking, code enhancer (B5.2)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.web_formatting import (
    format_message_content,
    format_tool_calls,
    sanitize_rendered_html,
)
from lore.interfaces.web_services import enrich_session_for_detail
from lore.utils.text_processing import format_thinking

_SESSION_ID = "sess-b52-markup"


def _index_entry() -> dict:
    return {
        "sessions": [
            {
                "id": _SESSION_ID,
                "tool": "claude-code",
                "title": "B5.2 markup session",
                "display_title": "B5.2 markup session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/lore",
                "messages": 2,
                "prompts": 1,
                "tokens": 100,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo/lore": 1},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }


def _synthetic_session() -> UnifiedSession:
    code_block = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(30)) + "\n```"
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=_SESSION_ID,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/lore",
        title="B5.2 markup session",
        messages=[
            UnifiedMessage(Role.USER, "Run tools please", datetime(2026, 4, 1, 10, 0, 0)),
            UnifiedMessage(
                Role.ASSISTANT,
                f"Here is code.\n{code_block}",
                datetime(2026, 4, 1, 10, 1, 0),
                reasoning="plan <script>alert(1)</script> step",
                tool_calls=[
                    {
                        "tool": "bash",
                        "input": {"command": "echo ok"},
                        "output": "done",
                        "status": "ok",
                    },
                    {
                        "tool": "bash",
                        "input": {"command": "false"},
                        "output": "command failed",
                        "status": "failed",
                    },
                ],
                tool_events=[
                    {"name": "bash", "status": "ok", "duration_ms": 340, "is_error": False},
                    {"name": "bash", "status": "failed", "duration_ms": 1200, "is_error": True},
                ],
            ),
        ],
    )


def _enriched_session():
    session = _synthetic_session()
    enrich_session_for_detail(
        session,
        _index_entry()["sessions"][0],
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    return session


@pytest.fixture()
def detail_client(monkeypatch):
    session = _enriched_session()
    monkeypatch.setattr(web, "load_index", lambda: _index_entry())
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, []),
    )
    return web.app.test_client()


def test_detail_page_has_no_chars_chip(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert re.search(r"\d+\s*chars", html, re.IGNORECASE) is None


def test_failed_tool_result_open_with_failed_word(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert "tool-result" in html
    failed_idx = html.find("command failed")
    assert failed_idx != -1
    window = html[max(0, failed_idx - 900) : failed_idx + 100]
    assert re.search(r"<details[^>]*\sopen", window)
    assert "failed" in window.lower()


def test_ok_tool_result_closed_with_ok_word(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    ok_idx = html.find(">done<")
    assert ok_idx != -1
    window = html[max(0, ok_idx - 1200) : ok_idx]
    assert "<details" in window
    assert " open" not in window.split("<details")[-1].split(">", 1)[0]
    assert re.search(r">\s*ok\s*<", window, re.IGNORECASE) or "status-ok" in window


def test_reasoning_in_think_fold_closed(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert '<details class="think-fold">' in html
    think_block = html.split('class="think-fold"', 1)[1].split("</details>", 1)[0]
    assert re.search(r"<details[^>]*\sopen", think_block) is None


def test_reasoning_escapes_html(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_sanitizer_keeps_details_summary():
    sample = '<details class="think-fold"><summary>Thinking</summary><div>x</div></details>'
    cleaned = sanitize_rendered_html(sample)
    assert "<details" in cleaned and "<summary" in cleaned


def test_base_template_single_code_enhancer_no_wrapper():
    base = Path(__file__).resolve().parents[1] / "lore" / "templates" / "base.html"
    text = base.read_text(encoding="utf-8")
    assert "code-block-wrapper" not in text
    assert text.count("function initCodeBlocks") == 1


def test_init_code_blocks_script_accessibility():
    base = Path(__file__).resolve().parents[1] / "lore" / "templates" / "base.html"
    text = base.read_text(encoding="utf-8")
    start = text.index("function initCodeBlocks")
    end = text.index("document.addEventListener('DOMContentLoaded', initCodeBlocks)")
    block = text[start:end]
    assert "aria-expanded" in block
    assert re.search(r"\.onclick\s*=", block) is None


def test_exit_code_is_never_interpolated_as_markup():
    from lore.utils.text_processing import format_tool_result

    html = format_tool_result(
        "boom",
        status="failed",
        exit_code="0</span><script>alert(1)</script>",  # type: ignore[arg-type]
    )
    assert "<script>" not in html


def test_duration_rejects_booleans_and_absurd_values():
    from lore.utils.text_processing import format_duration_ms

    assert format_duration_ms(True) is None
    assert format_duration_ms(10**5000) is None
    assert format_duration_ms(10**12) is None
    assert format_duration_ms(1500) == "1.5s"


def test_folded_calls_do_not_borrow_the_next_calls_event():
    from lore.interfaces.web_formatting import format_tool_calls

    calls = [
        {"tool": "Bash", "input": {"cmd": "ls"}, "_burst_count": 2},
        {"tool": "Read", "input": {"path": "/a"}},
    ]
    events = [
        {"name": "Bash", "status": "completed", "duration_ms": 10, "is_error": False},
        {"name": "Bash", "status": "failed", "duration_ms": 20, "is_error": True},
        {"name": "Read", "status": "completed", "duration_ms": 30, "is_error": False},
    ]

    html = format_tool_calls(calls, events)

    assert "failed" not in html  # the second Bash event must not label the Read call


def test_detail_mobile_prompt_outline_sheet(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert 'id="promptOutlineTrigger"' in html
    assert 'id="promptOutlineSheet"' in html
    assert 'aria-label="Prompt outline"' in html
    assert " hidden" in html or "hidden" in html.split("promptOutlineSheet", 1)[1][:80]


def test_detail_header_resume_primary_and_menu(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert 'id="sessionHeader"' in html
    assert 'class="session-header' in html
    assert 'data-action="resume-session"' in html
    assert re.search(
        r'class="[^"]*session-header-primary[^"]*"[^>]*>Resume<',
        html,
    ) or re.search(r">Resume</button>", html)
    assert '<details class="header-menu">' in html
    menu_block = html.split('<details class="header-menu">', 1)[1].split("</details>", 1)[0]
    assert "Copy Link" in menu_block or "copy-link" in menu_block
    assert "Export MD" in menu_block or "/export/" in menu_block
    assert "Dashboard" in menu_block or 'href="/"' in menu_block


def test_detail_delete_form_in_header_menu(detail_client):
    html = detail_client.get(f"/session/{_SESSION_ID}?live=1").get_data(as_text=True)
    assert f'action="/session/{_SESSION_ID}/delete"' in html
    assert "data-confirm=" in html
    delete_idx = html.find("/delete")
    assert delete_idx != -1
    window = html[max(0, delete_idx - 800) : delete_idx + 200]
    assert "header-menu" in window


def test_detail_sticky_header_css_in_base():
    base = Path(__file__).resolve().parents[1] / "lore" / "templates" / "base.html"
    text = base.read_text(encoding="utf-8")
    assert ".session-header" in text
    assert "position: sticky" in text or "position:sticky" in text.replace(" ", "")


def test_enhancer_reruns_on_appended_messages_and_checks_copy_result():
    from pathlib import Path

    source = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert "addEventListener('lore:messages-appended', initCodeBlocks)" in source
    assert "if (document.execCommand('copy'))" in source
    assert "area.style." not in source


def test_session_view_merged_controls_in_base_template():
    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    assert 'role="group"' in base and "set-view-tools" in base
    for old_id in (
        "cleanToggle",
        "ultraCleanToggle",
        "readabilityToggle",
        "presenterToggle",
        "densityToggle",
    ):
        assert old_id not in base


def test_sticky_header_wrap_owns_the_sticky_position_and_compacts_on_scroll():
    from pathlib import Path

    base = Path("lore/templates/base.html").read_text(encoding="utf-8")
    page = Path("lore/templates/session.html").read_text(encoding="utf-8")
    assert ".session-header-wrap { position: sticky; top: 0;" in base
    assert "session-header-sentinel" not in page
    assert "addEventListener('scroll'" in page and "requestAnimationFrame" in page
    assert "is-compact" in page


# --- #176: one exported enhancer, exact clipboard content -------------------


def _base_template_text() -> str:
    return (Path(__file__).resolve().parents[1] / "lore" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )


def _enhancer_block() -> str:
    text = _base_template_text()
    return text[text.index("function initCodeBlocks") : text.index("window.FocusTrap")]


def test_base_exports_the_enhancer_on_window():
    """One named, idempotent enhancer other templates can call (#176)."""
    assert "window.loreEnhanceCodeBlocks = initCodeBlocks" in _base_template_text()
    assert _enhancer_block().count("function initCodeBlocks") == 1
    # idempotency: an already-wrapped block is skipped on every re-run
    assert "el.closest('.code-card')" in _enhancer_block()


def test_copy_payload_is_the_exact_text_content():
    """`innerText` normalises whitespace and omits hidden text — the clipboard must not (#176)."""
    block = _enhancer_block()
    assert "const payload = el.textContent || '';" in block
    assert "el.innerText" not in block  # neither payload nor line count may rewrite the code


def test_session_loader_reuses_the_exported_enhancer():
    """The lazy loader must not run a second, partial enhancement path (#176)."""
    source = (
        Path(__file__).resolve().parents[1] / "lore" / "templates" / "session.html"
    ).read_text(encoding="utf-8")
    assert "window.loreEnhanceCodeBlocks" in source
    assert "pre code:not(.hljs)" not in source  # the local highlight loop is gone
    # appended pairs still announce themselves to the other listeners
    assert "window.dispatchEvent(new CustomEvent('lore:messages-appended'))" in source
