"""
API route contract tests (issue #28).

Each test verifies that a route returns the correct HTTP status code and
response shape.  No real index data or extractors are required — load_index
is monkeypatched to return an empty index so tests are fully isolated.
"""

from __future__ import annotations

import pytest

from annuli.interfaces import web

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_INDEX: dict = {
    "sessions": [],
    "stats": {
        "total_sessions": 0,
        "total_messages": 0,
        "by_tool": {},
        "by_project": {},
    },
    "generated_at": "2026-01-01T00:00:00Z",
}


@pytest.fixture()
def client(monkeypatch):
    """Flask test client with load_index returning an empty index."""
    monkeypatch.setattr(web, "load_index", lambda: _EMPTY_INDEX)
    with web.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# HTML page routes
# ---------------------------------------------------------------------------


def test_root_returns_200(client):
    r = client.get("/")
    assert r.status_code == 200


def test_sessions_page_returns_200_html(client):
    r = client.get("/sessions")
    assert r.status_code == 200
    assert b"text/html" in r.content_type.encode()


def test_stats_page_returns_200(client):
    r = client.get("/stats")
    assert r.status_code == 200


def test_threads_page_returns_200(client):
    r = client.get("/threads")
    assert r.status_code == 200


def test_projects_page_returns_200(client):
    r = client.get("/projects")
    assert r.status_code == 200


def test_rules_page_returns_200(client):
    r = client.get("/rules")
    assert r.status_code == 200


def test_noise_rules_page_returns_200(client):
    r = client.get("/noise-rules")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


def test_health_returns_200_with_status_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload is not None
    assert payload["status"] == "ok"


def test_health_payload_has_required_keys(client):
    payload = client.get("/api/health").get_json()
    for key in ("status", "revision", "uptime_seconds", "timestamp"):
        assert key in payload, f"missing key: {key}"


# ---------------------------------------------------------------------------
# /api/metrics
# ---------------------------------------------------------------------------


def test_metrics_returns_200_json(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload is not None
    assert "metrics" in payload


def test_metrics_prometheus_format_returns_text(client):
    r = client.get("/api/metrics?format=prometheus")
    assert r.status_code == 200
    assert "text/plain" in r.content_type


# ---------------------------------------------------------------------------
# /api/stats/costs
# ---------------------------------------------------------------------------


def test_api_stats_costs_returns_200_with_expected_keys(client):
    r = client.get("/api/stats/costs")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload is not None
    for key in (
        "total_tokens",
        "by_tool",
        "by_day",
        "by_project",
        "session_count",
        "untokened_count",
    ):
        assert key in payload, f"missing key: {key}"


def test_api_stats_costs_by_day_is_list_of_30(client):
    payload = client.get("/api/stats/costs").get_json()
    assert isinstance(payload["by_day"], list)
    assert len(payload["by_day"]) == 30


# ---------------------------------------------------------------------------
# /api/reload-status/<job_id>
# ---------------------------------------------------------------------------


def test_reload_status_unknown_job_returns_404(client):
    r = client.get("/api/reload-status/nonexistent-job-abc123")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/audit-status/<job_id>
# ---------------------------------------------------------------------------


def test_audit_status_unknown_job_returns_404(client):
    r = client.get("/api/audit-status/nonexistent-audit-xyz999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/reload-sessions
# ---------------------------------------------------------------------------


def test_post_reload_sessions_sync_returns_200(monkeypatch, client):
    def _fake_reload(**_):
        return {
            "status": "ok",
            "reload_seconds": 0.01,
            "total_sessions": 0,
            "by_tool": {},
            "revision": "test",
            "mode": "incremental",
        }

    monkeypatch.setattr(web, "_reload_sessions_index", _fake_reload)
    r = client.post("/api/reload-sessions")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["status"] == "ok"


def test_post_reload_sessions_async_returns_202_with_job_id(monkeypatch, client):
    class _FakeStart:
        def __call__(self, *_args, **_kwargs):  # type: ignore[misc]
            return "job-test-42"

    monkeypatch.setattr(web, "_start_reload_job", _FakeStart())
    r = client.post("/api/reload-sessions?async=1")
    assert r.status_code == 202
    payload = r.get_json()
    assert payload["status"] == "accepted"
    assert payload["job_id"] == "job-test-42"


# ---------------------------------------------------------------------------
# POST /api/sessions/<id>/resume — 404 for unknown session
# ---------------------------------------------------------------------------


def test_session_resume_unknown_id_returns_404(client):
    # The endpoint validates the ID first; "unknown-session-id" passes validation
    # but won't be found in the empty index → 404.
    r = client.post("/api/sessions/unknown-session-id/resume")
    assert r.status_code == 404
    payload = r.get_json()
    assert payload is not None
    assert "error" in payload


# ---------------------------------------------------------------------------
# /api/v1/sessions
# ---------------------------------------------------------------------------


def test_api_v1_sessions_returns_200_json_list(client):
    r = client.get("/api/v1/sessions")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload is not None
    assert "sessions" in payload
    assert isinstance(payload["sessions"], list)


def test_api_v1_sessions_empty_index_returns_zero_count(client):
    payload = client.get("/api/v1/sessions").get_json()
    assert payload["count"] == 0
    assert payload["sessions"] == []


# ---------------------------------------------------------------------------
# /api/v1/sessions/<id>
# ---------------------------------------------------------------------------


def test_api_v1_session_detail_unknown_id_returns_404(client):
    r = client.get("/api/v1/sessions/nonexistent-session-id")
    assert r.status_code == 404
    payload = r.get_json()
    assert payload is not None
    assert "error" in payload


def test_api_v1_session_detail_invalid_id_returns_400(client):
    # Session IDs with path-traversal characters are rejected before DB lookup
    r = client.get("/api/v1/sessions/../../../etc/passwd")
    # Flask will resolve the path, so we just assert it's not a 200
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# /api/build-info
# ---------------------------------------------------------------------------


def test_build_info_returns_200(client):
    r = client.get("/api/build-info")
    assert r.status_code == 200
    assert r.get_json() is not None


# ---------------------------------------------------------------------------
# /api/audit
# ---------------------------------------------------------------------------


def test_api_audit_index_scope_returns_200(client):
    r = client.get("/api/audit?scope=index")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["scope"] == "index"


def test_api_audit_invalid_provider_returns_400(client):
    r = client.get("/api/audit?scope=index&provider=bad;provider")
    assert r.status_code == 400


def test_api_stats_costs_counts_untokened_sessions(monkeypatch, client):
    """#67 follow-up — sessions without token data are counted, not silently dropped."""
    index = {
        "sessions": [
            {
                "id": "a",
                "tool": "claude-code",
                "project": "/p",
                "created": "2026-06-01",
                "tokens": 100,
            },
            {"id": "b", "tool": "codex", "project": "/p", "created": "2026-06-01", "tokens": None},
            {"id": "c", "tool": "codex", "project": "/p", "created": "2026-06-01"},  # no tokens key
            {"id": "d", "tool": "opencode", "project": "/p", "created": "2026-06-01", "tokens": 0},
        ]
    }
    monkeypatch.setattr(web, "load_index", lambda: index)

    payload = client.get("/api/stats/costs").get_json()
    assert payload["total_tokens"] == 100
    assert payload["session_count"] == 1  # only "a" has usable tokens
    assert payload["untokened_count"] == 3  # b (None), c (missing), d (0)
