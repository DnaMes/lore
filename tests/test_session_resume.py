"""Tests for POST /api/sessions/<session_id>/resume endpoint."""

from lore.interfaces import web


def _index_with_session(session_id: str, tool: str, project: str = "/repo/demo") -> dict:
    return {
        "sessions": [
            {
                "id": session_id,
                "tool": tool,
                "title": "Test session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": project,
                "messages": 2,
                "prompts": 1,
                "export_path": None,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {tool: 1},
            "by_project": {project: 1},
        },
    }


def test_resume_claude_code_returns_command(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with_session("ses-abc123", "claude-code"))

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-abc123/resume")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is True
    assert "claude --resume ses-abc123" in data["command"]
    assert "/repo/demo" in data["command"]
    assert data["project"] == "/repo/demo"
    assert data["tool"] == "claude-code"


def test_resume_claude_code_no_project(monkeypatch):
    idx = _index_with_session("ses-abc123", "claude-code", project="")
    idx["sessions"][0]["project"] = None
    monkeypatch.setattr(web, "load_index", lambda: idx)

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-abc123/resume")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is True
    assert data["command"] == "claude --resume ses-abc123"
    assert data["project"] is None


def test_resume_opencode_returns_command(monkeypatch):
    monkeypatch.setattr(
        web, "load_index", lambda: _index_with_session("ses-opencode-01", "opencode")
    )

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-opencode-01/resume")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is True
    assert "opencode --resume ses-opencode-01" in data["command"]
    assert data["tool"] == "opencode"


def test_resume_cursor_not_supported(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with_session("ses-cursor-01", "cursor"))

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-cursor-01/resume")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is False
    assert data["command"] is None
    assert "Cursor" in data["reason"] or "cursor" in data["reason"].lower()


def test_resume_unknown_tool_not_supported(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_with_session("ses-cursor-xyz1", "cursor"))

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-cursor-xyz1/resume")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is False
    assert data["command"] is None


def test_resume_session_not_found(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": [], "stats": {}})

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/ses-missing1/resume")

    assert resp.status_code == 404
    data = resp.get_json()
    assert "error" in data


def test_resume_invalid_session_id(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: {"sessions": [], "stats": {}})

    with web.app.test_client() as client:
        resp = client.post("/api/sessions/../etc/passwd/resume")

    # Either 400 (rejected as invalid ID) or 404 / 308 (route mismatch) is acceptable
    assert resp.status_code in (400, 404, 308)


def _session_template_html() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "lore" / "templates" / "session.html"
    ).read_text(encoding="utf-8")


def test_resume_button_present_in_session_template():
    """The Resume button must be rendered in the session detail page HTML."""
    html = _session_template_html()

    assert "resumeSession" in html
    assert "Resume" in html


def test_resume_modal_js_present_in_session_template():
    """The modal and copy helper JS must be embedded in the session template."""
    html = _session_template_html()

    assert "resume-modal" in html
    assert "closeResumeModal" in html
    assert "copyResumeCmd" in html
    assert "/api/sessions/" in html
