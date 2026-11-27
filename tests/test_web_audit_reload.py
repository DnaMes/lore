import time

from annuli.interfaces import web, web_jobs


def test_api_audit_index_returns_uniformity_payload(monkeypatch):
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: {
            "sessions": [
                {
                    "id": "ses-1",
                    "tool": "opencode",
                    "title": "Hello",
                    "thread_id": "thread-1",
                    "messages": 3,
                    "prompts": 1,
                }
            ]
        },
    )

    with web.app.test_client() as client:
        response = client.get("/api/audit?scope=index")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scope"] == "index"
    assert payload["totals"]["sessions"] == 1
    assert "opencode" in payload["by_tool"]


def test_api_reload_sessions_returns_status(monkeypatch):
    monkeypatch.setattr(
        web,
        "_reload_sessions_index",
        lambda **kwargs: {
            "status": "ok",
            "reload_seconds": 0.123,
            "total_sessions": 42,
            "by_tool": {"opencode": 10},
            "revision": "test",
            "mode": "incremental" if kwargs.get("incremental", True) else "full",
        },
    )

    with web.app.test_client() as client:
        response = client.post("/api/reload-sessions")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["total_sessions"] == 42
    assert payload["mode"] == "incremental"

    with web.app.test_client() as client:
        full_response = client.post("/api/reload-sessions?full=1")

    assert full_response.status_code == 200
    assert full_response.get_json()["mode"] == "full"


def test_api_audit_rejects_invalid_provider():
    with web.app.test_client() as client:
        response = client.get("/api/audit?scope=index&provider=bad;provider")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid provider"


def test_api_reload_accepts_async_job(monkeypatch):
    captured: dict = {}

    def fake_start(provider, incremental=True):
        captured["provider"] = provider
        captured["incremental"] = incremental
        return "reload-job-1"

    monkeypatch.setattr(web, "_start_reload_job", fake_start)

    with web.app.test_client() as client:
        response = client.post("/api/reload-sessions?async=1&provider=opencode")

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "accepted"
    assert payload["job_id"] == "reload-job-1"
    assert payload["mode"] == "incremental"
    assert captured["incremental"] is True

    with web.app.test_client() as client:
        full_response = client.post("/api/reload-sessions?async=1&full=1")

    assert full_response.status_code == 202
    assert full_response.get_json()["mode"] == "full"
    assert captured["incremental"] is False


def test_api_reload_status_not_found():
    with web.app.test_client() as client:
        response = client.get("/api/reload-status/reload-missing")

    assert response.status_code == 404


def test_api_audit_accepts_async_job(monkeypatch):
    monkeypatch.setattr(web, "_start_audit_job", lambda scope, provider: "audit-job-1")

    with web.app.test_client() as client:
        response = client.get("/api/audit?scope=live&async=1&provider=opencode")

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "accepted"
    assert payload["job_id"] == "audit-job-1"
    assert payload["scope"] == "live"


def test_api_audit_status_not_found():
    with web.app.test_client() as client:
        response = client.get("/api/audit-status/audit-missing")

    assert response.status_code == 404


def test_api_action_cancel_marks_running_job(monkeypatch):
    now = time.time()
    with web.RELOAD_JOBS_LOCK:
        web.RELOAD_JOBS.clear()
        web.RELOAD_JOBS["reload-active"] = {
            "job_id": "reload-active",
            "kind": "reload",
            "status": "running",
            "cancel_requested": False,
            "started_epoch": now,
            "updated_epoch": now,
            "updated_at": "2026-01-01T00:00:00Z",
        }

    with web.app.test_client() as client:
        response = client.post("/api/action-cancel/reload-active")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "accepted"
    with web.RELOAD_JOBS_LOCK:
        assert web.RELOAD_JOBS["reload-active"]["cancel_requested"] is True


def test_assert_job_active_times_out(monkeypatch):
    now = time.time()
    monkeypatch.setattr(web_jobs, "ACTION_JOB_TIMEOUT_SECONDS", 1)
    with web.RELOAD_JOBS_LOCK:
        web.RELOAD_JOBS.clear()
        web.RELOAD_JOBS["reload-timeout"] = {
            "job_id": "reload-timeout",
            "kind": "reload",
            "status": "running",
            "cancel_requested": False,
            "started_epoch": now - 5,
            "updated_epoch": now - 5,
            "updated_at": "2026-01-01T00:00:00Z",
        }

    try:
        web._assert_job_active("reload-timeout")
        raised = False
    except web.ActionJobTimeoutError:
        raised = True

    assert raised is True
    state = web._get_reload_job("reload-timeout")
    assert state is not None
    assert state.get("error") == "timeout"


def test_reload_job_prunes_stale_done_jobs(monkeypatch):
    now = time.time()
    monkeypatch.setattr(web_jobs, "RELOAD_JOB_TTL_SECONDS", 1)
    monkeypatch.setattr(web_jobs, "RELOAD_JOB_MAX", 256)

    with web.RELOAD_JOBS_LOCK:
        web.RELOAD_JOBS.clear()
        web.RELOAD_JOBS["done-old"] = {
            "job_id": "done-old",
            "kind": "reload",
            "status": "done",
            "updated_epoch": now - 10,
            "updated_at": "2026-01-01T00:00:00Z",
        }
        web.RELOAD_JOBS["running-old"] = {
            "job_id": "running-old",
            "kind": "reload",
            "status": "running",
            "updated_epoch": now - 10,
            "updated_at": "2026-01-01T00:00:00Z",
        }

    assert web._get_reload_job("missing") is None

    with web.RELOAD_JOBS_LOCK:
        assert "done-old" not in web.RELOAD_JOBS
        assert "running-old" in web.RELOAD_JOBS
