import json
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.web_data import _annotate_display_titles


def test_annotate_display_titles_deduplicates_same_title():
    sessions = [
        {"id": "aaaa1111-bbbb", "title": "Warmup"},
        {"id": "cccc2222-dddd", "title": "Warmup"},
    ]

    annotated = _annotate_display_titles(sessions)

    assert annotated[0]["display_title"] == "Warmup · aaaa1111"
    assert annotated[1]["display_title"] == "Warmup · cccc2222"


def test_annotate_display_titles_uses_id_for_missing_title():
    sessions = [{"id": "1234567890abcdef", "title": ""}]

    annotated = _annotate_display_titles(sessions)

    assert annotated[0]["display_title"] == "1234567890ab"


def test_render_escapes_recent_titles(monkeypatch):
    malicious_title = "<command-name>login</command-name>"
    sessions = [
        {
            "id": "abc12345-def6-7890-abcd-ef1234567890",
            "tool": "opencode",
            "title": malicious_title,
            "display_title": malicious_title,
            "created": "2026-01-01T00:00:00",
            "prompts": 1,
            "messages": 1,
            "project": "",
        }
    ]
    stats = {
        "total_sessions": 1,
        "total_messages": 1,
        "by_tool": {"opencode": 1},
        "by_project": {},
    }

    monkeypatch.setattr(web, "load_index", lambda: {"sessions": sessions, "stats": stats})

    page = web.render(
        "dashboard",
        active="dashboard",
        stats=stats,
        recent_sessions=sessions,
        title="Dashboard",
    )

    assert "&lt;command-name&gt;login&lt;/command-name&gt;" in page
    assert "<command-name>login</command-name>" not in page


def test_format_message_content_escapes_when_markdown_unavailable(monkeypatch):
    from lore.interfaces import web_formatting

    monkeypatch.setattr(web_formatting, "markdown", None)

    rendered = web.format_message_content("<script>alert(1)</script>\nnext")

    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<br>" in rendered


def test_format_message_content_strips_javascript_href():
    rendered = web.format_message_content("[open](javascript:alert(1))")

    assert "javascript:alert(1)" not in rendered
    assert "<a" in rendered


def test_rules_view_escapes_when_markdown_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web, "markdown", None)
    (tmp_path / "rules.md").write_text("<img src=x onerror=alert(1)>", encoding="utf-8")

    with web.app.test_request_context("/rules"):
        page = web.rules()

    assert "<img src=x onerror=alert(1)>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_thread_continue_command_is_attribute_escaped():
    """The continue command must not be able to break out of its attribute.

    #106 moved the payload out of an inline onclick (JS context, |tojson
    escaping) into a data-copy-text attribute (HTML attribute context,
    autoescape). The adversarial quote must therefore arrive as &quot;
    inside the double-quoted attribute — never as a raw quote.
    """
    cmd = 'ai-session switch gemini --thread-id bad";alert(1)//'

    page = web.render(
        "thread_detail",
        active="threads",
        thread_id="thread-123",
        continue_cmd=cmd,
        messages=[],
        toc_items=[],
        thread_meta={
            "session_count": 0,
            "message_count": 0,
            "tools": 0,
            "start_date": "n/a",
            "end_date": "n/a",
        },
        thread_groups=[],
        thread_timeline=[],
        title="Thread",
    )

    assert 'data-copy-text="ai-session switch gemini --thread-id bad&#34;;alert(1)//"' in page
    assert 'data-copy-text="ai-session switch gemini --thread-id bad"' not in page


def test_threads_list_urlencodes_thread_links():
    page = web.render(
        "threads",
        active="threads",
        threads=[
            {
                "thread_id": "project:abc 123/unsafe",
                "title": "Thread",
                "project": "demo",
                "count": 1,
                "updated": "2026-01-01T00:00:00",
            }
        ],
        title="Threads",
    )

    assert "/thread/project%3Aabc%20123/unsafe" not in page
    assert "/thread/project%3Aabc%20123%2Funsafe" in page


def test_thread_detail_rejects_invalid_thread_id():
    with web.app.test_client() as client:
        response = client.get("/thread/bad;id")

    assert response.status_code == 400


def test_thread_detail_returns_404_for_unknown_thread_id():
    with web.app.test_client() as client:
        response = client.get("/thread/thread-id-does-not-exist-xyz")

    assert response.status_code == 404


def test_session_template_urlencodes_export_link():
    page = web.render(
        "session",
        active="sessions",
        session={
            "title": "t",
            "created_at": type("_D", (), {"strftime": lambda self, fmt: "2026-01-01 00:00"})(),
            "project_path": "",
            "prompt_count": 1,
            "thread_id": "thread-1",
            "session_id": "project:abc 123/unsafe",
            "pairs": [],
            "visible_count": 0,
            "tool": type("_Tool", (), {"value": "opencode"})(),
        },
        style=web.get_style("opencode"),
        title="Session",
        toc_items=[],
    )

    assert "/export/project%3Aabc%20123/unsafe" not in page
    assert "/export/project%3Aabc%20123%2Funsafe" in page


def test_security_headers_include_referrer_and_stricter_csp():
    with web.app.test_client() as client:
        response = client.get("/")

    csp = response.headers.get("Content-Security-Policy", "")
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("X-AI-History-Revision")


def test_api_build_info_exposes_revision_and_hardening(monkeypatch):
    monkeypatch.setenv("LORE_BUILD_SHA", "abc123")
    monkeypatch.delenv("LORE_EXPORT_FALLBACK_SCAN", raising=False)

    with web.app.test_client() as client:
        response = client.get("/api/build-info")

    assert response.status_code == 200
    payload = response.get_json()
    # #119 moved the builder to web_audit.py — the payload reports the
    # module that actually built it.
    assert payload["module"] == "lore.interfaces.web_audit"
    assert payload["revision"] == "abc123"
    assert payload["hardening"]["thread_unknown_returns_404"] is True
    assert payload["hardening"]["search_param_validation"] is True
    assert payload["hardening"]["export_unknown_returns_404_by_default"] is True
    assert payload["export_fallback_scan_enabled"] is False


def test_build_info_reports_package_version():
    """build-info exposes the single-source-of-truth package version."""
    from lore import __version__

    with web.app.test_client() as client:
        response = client.get("/api/build-info")
    assert response.get_json()["version"] == __version__


def test_version_shown_in_sidebar():
    """The dashboard sidebar footer displays the app version."""
    from lore import __version__

    with web.app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert f"lore v{__version__}" in page


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/session/bad;id", 400),
        ("/session/session-id-does-not-exist-xyz", 404),
        ("/thread/bad;id", 400),
        ("/thread/thread-id-does-not-exist-xyz", 404),
        ("/export/bad;id", 400),
        ("/export/session-id-does-not-exist-xyz", 404),
    ],
)
def test_route_probe_matrix_statuses(monkeypatch, path, expected_status):
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": []})
    monkeypatch.setattr(web, "get_all_extractors", lambda: [])

    with web.app.test_client() as client:
        response = client.get(path)

    assert response.status_code == expected_status


def test_export_unknown_does_not_scan_extractors_by_default(monkeypatch):
    monkeypatch.delenv("LORE_EXPORT_FALLBACK_SCAN", raising=False)
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": []})

    called = {"value": False}

    def _unexpected_call():
        called["value"] = True
        return []

    monkeypatch.setattr(web, "get_all_extractors", _unexpected_call)

    with web.app.test_client() as client:
        response = client.get("/export/session-id-does-not-exist-xyz")

    assert response.status_code == 404
    assert called["value"] is False


def test_export_unknown_scan_opt_in_calls_extractors(monkeypatch):
    monkeypatch.setenv("LORE_EXPORT_FALLBACK_SCAN", "true")
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": []})

    called = {"value": False}

    def _extractor_call():
        called["value"] = True
        return []

    monkeypatch.setattr(web, "get_all_extractors", _extractor_call)

    with web.app.test_client() as client:
        response = client.get("/export/session-id-does-not-exist-xyz")

    assert response.status_code == 404
    assert called["value"] is True


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_fragment"),
    [
        ("/api/search?q=a", 200, "[]"),
        ("/api/search?q=ok;drop", 400, "Invalid search query"),
        ("/api/search?q=ok&tool=bad;tool", 400, "Invalid tool parameter"),
        ("/api/search?q=ok&project=bad;project", 400, "Invalid project parameter"),
    ],
)
def test_api_search_probe_matrix_statuses(path, expected_status, expected_fragment):
    with web.app.test_client() as client:
        response = client.get(path)

    assert response.status_code == expected_status
    assert expected_fragment in response.get_data(as_text=True)


def test_session_detail_prefers_markdown_by_default(monkeypatch, tmp_path):
    session_id = "ses-live-first"
    markdown_file = tmp_path / "session.md"
    markdown_file.write_text("# from-markdown\n\n## Conversation\n", encoding="utf-8")

    idx = {
        "sessions": [
            {
                "id": session_id,
                "tool": "warp",
                "title": "stale title",
                "created": "2026-03-01T10:00:00",
                "updated": "2026-03-01T10:01:00",
                "messages": 2,
                "prompts": 1,
                "project": "/repo",
                "thread_id": "thread-1",
                "export_path": str(markdown_file),
            }
        ]
    }
    monkeypatch.setattr(web, "load_index", lambda: idx)
    monkeypatch.setattr(web, "resolve_export_path", lambda _value: markdown_file)

    with web.app.test_client() as client:
        response = client.get(f"/session/{session_id}")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "from-markdown" in body


def test_session_detail_live_mode_prefers_live_extractor(monkeypatch, tmp_path):
    session_id = "ses-live-first"
    markdown_file = tmp_path / "session.md"
    markdown_file.write_text("# from-markdown\n\n## Conversation\n", encoding="utf-8")

    idx = {
        "sessions": [
            {
                "id": session_id,
                "tool": "warp",
                "title": "stale title",
                "created": "2026-03-01T10:00:00",
                "updated": "2026-03-01T10:01:00",
                "messages": 2,
                "prompts": 1,
                "project": "/repo",
                "thread_id": "thread-1",
                "export_path": str(markdown_file),
            }
        ]
    }
    monkeypatch.setattr(web, "load_index", lambda: idx)

    live_session = UnifiedSession(
        tool=Tool.WARP,
        session_id=session_id,
        created_at=datetime(2026, 3, 1, 10, 0, 0),
        last_updated=datetime(2026, 3, 1, 10, 1, 0),
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="user question",
                timestamp=datetime(2026, 3, 1, 10, 0, 0),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="assistant answer",
                timestamp=datetime(2026, 3, 1, 10, 0, 30),
            ),
        ],
        title="live title",
    )
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [live_session])
    monkeypatch.setattr(web, "resolve_export_path", lambda _value: markdown_file)

    with web.app.test_client() as client:
        response = client.get(f"/session/{session_id}?live=1")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "assistant answer" in body
    assert "live title" in body


def test_session_detail_prefers_live_when_markdown_has_no_assistant(monkeypatch, tmp_path):
    session_id = "ses-warp-stale-md"
    markdown_file = tmp_path / "session.md"
    markdown_file.write_text(
        "# stale-md\n\n## Conversation\n\n> user only\n",
        encoding="utf-8",
    )

    idx = {
        "sessions": [
            {
                "id": session_id,
                "tool": "warp",
                "title": "stale title",
                "created": "2026-03-01T10:00:00",
                "updated": "2026-03-01T10:01:00",
                "messages": 2,
                "prompts": 2,
                "project": "/repo",
                "thread_id": "thread-1",
                "export_path": str(markdown_file),
            }
        ]
    }
    monkeypatch.setattr(web, "load_index", lambda: idx)
    monkeypatch.setattr(web, "resolve_export_path", lambda _value: markdown_file)

    live_session = UnifiedSession(
        tool=Tool.WARP,
        session_id=session_id,
        created_at=datetime(2026, 3, 1, 10, 0, 0),
        last_updated=datetime(2026, 3, 1, 10, 1, 0),
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="kannst du das system in 1h herunterfahren!",
                timestamp=datetime(2026, 3, 1, 10, 0, 0),
            ),
            UnifiedMessage(
                role=Role.USER,
                content="ja",
                timestamp=datetime(2026, 3, 1, 10, 0, 20),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="Das System wird in 60 Minuten heruntergefahren.",
                timestamp=datetime(2026, 3, 1, 10, 0, 35),
            ),
        ],
        title="live warp title",
    )
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [live_session])

    with web.app.test_client() as client:
        response = client.get(f"/session/{session_id}")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Das System wird in 60 Minuten heruntergefahren." in body
    assert "live warp title" in body


def test_dashboard_renders_all_known_tools_even_when_stats_empty(monkeypatch):
    stats = {
        "total_sessions": 0,
        "total_messages": 0,
        "by_tool": {},
        "by_project": {},
    }
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": [], "stats": stats})

    with web.app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "VSCode Copilot" in body
    assert "Antigravity" in body


def test_session_delete_rejects_invalid_session_id():
    with web.app.test_client() as client:
        response = client.post("/session/bad;id/delete")

    assert response.status_code == 400
    assert "Invalid session ID" in response.get_data(as_text=True)


def test_session_delete_removes_index_entry_and_export(monkeypatch, tmp_path):
    from lore.interfaces import web_data

    session_id = "test-session-1"
    # export_path must live inside OUTPUT_DIR so resolve_export_path accepts it
    fake_output_dir = tmp_path / ".ai-history"
    fake_output_dir.mkdir()
    export_path = fake_output_dir / "session.md"
    export_path.write_text("# Session", encoding="utf-8")
    index_path = fake_output_dir / "index.json"
    index_payload = {
        "stats": {
            "total_sessions": 2,
            "total_messages": 6,
            "by_tool": {"antigravity": 1, "codex": 1},
            "by_project": {"/p": 1},
        },
        "sessions": [
            {
                "id": session_id,
                "tool": "antigravity",
                "project": "/p",
                "messages": 3,
                "export_path": str(export_path),
            },
            {
                "id": "test-session-2",
                "tool": "codex",
                "project": None,
                "messages": 3,
                "export_path": None,
            },
        ],
    }
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")

    monkeypatch.setattr(web_data, "OUTPUT_DIR", fake_output_dir)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", fake_output_dir / "deleted.json")
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    with web.app.test_client() as client:
        response = client.post(f"/session/{session_id}/delete")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/sessions")
    # The session's export markdown is removed.
    assert export_path.exists() is False

    # Deletion is tombstone-based (#44): the session is recorded as deleted
    # and load_index() no longer surfaces it. The index.json is not rewritten
    # in place — the tombstone filter does the work.
    web_data.clear_index_cache()
    remaining = [s["id"] for s in web_data.load_index().get("sessions", [])]
    assert session_id not in remaining
    assert "test-session-2" in remaining


def test_export_session_builds_markdown_from_live_indexed_session(monkeypatch, tmp_path):
    session_id = "ses-export-live"
    index_path = tmp_path / "index.json"
    index_payload = {
        "stats": {},
        "sessions": [
            {
                "id": session_id,
                "tool": "warp",
                "export_path": None,
            }
        ],
    }
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")

    live_session = UnifiedSession(
        tool=Tool.WARP,
        session_id=session_id,
        created_at=datetime(2026, 3, 1, 10, 0, 0),
        last_updated=datetime(2026, 3, 1, 10, 1, 0),
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="user question",
                timestamp=datetime(2026, 3, 1, 10, 0, 0),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="assistant answer",
                timestamp=datetime(2026, 3, 1, 10, 0, 30),
            ),
        ],
        title="live title",
    )

    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [live_session])

    with web.app.test_client() as client:
        response = client.get(f"/export/{session_id}")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "# live title" in body
    assert "assistant answer" in body


def test_session_template_includes_delete_button():
    page = web.render(
        "session",
        active="sessions",
        session={
            "title": "t",
            "created_at": type("_D", (), {"strftime": lambda self, fmt: "2026-01-01 00:00"})(),
            "project_path": "",
            "prompt_count": 1,
            "thread_id": "thread-1",
            "session_id": "session-123",
            "pairs": [],
            "visible_count": 0,
            "tool": type("_Tool", (), {"value": "opencode"})(),
        },
        style=web.get_style("opencode"),
        title="Session",
        toc_items=[],
    )

    assert "/session/session-123/delete" in page
    assert "Delete</button>" in page


def test_base_template_includes_reload_and_audit_controls(monkeypatch):
    stats = {"total_sessions": 0, "total_messages": 0, "by_tool": {}, "by_project": {}}
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": [], "stats": stats})

    with web.app.test_client() as client:
        response = client.get("/")

    body = response.get_data(as_text=True)
    assert 'id="syncBtn"' in body
    assert 'id="cancelActionBtn"' in body
    assert "All providers" in body
    assert "Quick (index)" in body
    assert "Deep (live)" in body
    # Formatting buttons should NOT be present on dashboard
    assert 'id="readabilityToggle"' not in body
    assert 'id="ultraCleanToggle"' not in body
    assert 'id="presenterToggle"' not in body
    assert 'id="cleanToggle"' not in body
    assert 'id="densityToggle"' not in body


def test_base_template_exposes_accessibility_attributes():
    with web.app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="actionStatus" role="status" aria-live="polite" aria-atomic="true"' in body
    assert 'id="searchModal" role="dialog" aria-modal="true"' in body
    # Formatting buttons not on dashboard, but theme toggle is always present
    assert (
        'id="themeToggle" type="button" aria-label="Toggle dark mode" aria-pressed="false"' in body
    )


def test_base_template_has_error_toast_function():
    page = web.render("base", active="dashboard", title="Test")

    assert "function showErrorToast(title, message)" in page
    assert "toast.id = 'errorToast'" in page


def test_health_and_ready_endpoints_return_expected_shape():
    with web.app.test_client() as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")

    assert health.status_code == 200
    health_payload = health.get_json()
    assert health_payload["status"] == "ok"
    assert "uptime_seconds" in health_payload
    assert "revision" in health_payload

    assert ready.status_code in {200, 503}
    ready_payload = ready.get_json()
    assert "ready" in ready_payload
    assert "checks" in ready_payload
    assert "output_dir_writable" in ready_payload["checks"]


def test_api_responses_include_request_id_header():
    with web.app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert request_id.startswith("req-")


def test_api_rate_limit_enforced_when_enabled(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("LORE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("LORE_RATE_LIMIT_SEARCH_PER_WINDOW", "2")
    web.RATE_LIMIT_STATE.clear()

    with web.app.test_client() as client:
        first = client.get("/api/search?q=ab")
        second = client.get("/api/search?q=ab")
        third = client.get("/api/search?q=ab")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    payload = third.get_json()
    assert payload["error"] == "Rate limit exceeded"
    assert int(payload["retry_after_seconds"]) >= 1
    assert third.headers.get("Retry-After")


def test_api_rate_limit_disabled_when_configured(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("LORE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("LORE_RATE_LIMIT_SEARCH_PER_WINDOW", "1")
    web.RATE_LIMIT_STATE.clear()

    with web.app.test_client() as client:
        first = client.get("/api/search?q=ab")
        second = client.get("/api/search?q=ab")
        third = client.get("/api/search?q=ab")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200


def test_api_build_info_includes_new_hardening_flags():
    with web.app.test_client() as client:
        response = client.get("/api/build-info")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["hardening"]["request_id_header"] is True
    assert payload["hardening"]["api_rate_limiting"] is True
    assert payload["hardening"]["health_ready_endpoints"] is True
    assert payload["hardening"]["metrics_endpoint"] is True


def test_api_rate_limit_headers_present_on_api_responses(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("LORE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("LORE_RATE_LIMIT_SEARCH_PER_WINDOW", "5")
    web.RATE_LIMIT_STATE.clear()

    with web.app.test_client() as client:
        response = client.get("/api/search?q=ab")

    assert response.status_code == 200
    assert response.headers.get("X-RateLimit-Limit") == "5"
    remaining = int(response.headers.get("X-RateLimit-Remaining") or "-1")
    assert 0 <= remaining <= 4
    reset = int(response.headers.get("X-RateLimit-Reset") or "0")
    assert reset >= 1


def test_metrics_endpoint_returns_counters(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "false")
    web.RATE_LIMIT_STATE.clear()
    with web.METRICS_LOCK:
        for key in list(web.METRICS.keys()):
            web.METRICS[key] = 0

    with web.app.test_client() as client:
        _ = client.get("/api/health")
        _ = client.get("/api/search?q=ab")
        response = client.get("/api/metrics")

    assert response.status_code == 200
    payload = response.get_json()
    assert "metrics" in payload
    metrics = payload["metrics"]
    assert metrics["requests_total"] >= 2
    assert metrics["api_requests_total"] >= 2
    assert "responses_2xx" in metrics
    assert "rate_limit_rejections" in metrics


def test_metrics_endpoint_supports_prometheus_text(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "false")
    web.RATE_LIMIT_STATE.clear()
    with web.METRICS_LOCK:
        for key in list(web.METRICS.keys()):
            web.METRICS[key] = 0

    with web.app.test_client() as client:
        _ = client.get("/api/health")
        response = client.get("/api/metrics?format=prom")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    body = response.get_data(as_text=True)
    assert "# HELP lore_uptime_seconds" in body
    assert "lore_requests_total" in body


def test_record_job_outcome_updates_job_metrics(monkeypatch):
    monkeypatch.setenv("LORE_RATE_LIMIT_ENABLED", "false")
    with web.METRICS_LOCK:
        for key in list(web.METRICS.keys()):
            web.METRICS[key] = 0

    start = web.time.time() - 0.05
    web._record_job_outcome("reload", "completed", start)
    web._record_job_outcome("audit", "failed", start)

    snapshot = web._metrics_snapshot()
    assert snapshot["reload_jobs_completed"] == 1
    assert snapshot["audit_jobs_failed"] == 1
    assert snapshot["reload_jobs_duration_ms_total"] >= 1
    assert snapshot["audit_jobs_duration_ms_total"] >= 1
