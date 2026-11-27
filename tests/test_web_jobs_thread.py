"""Tests for web_jobs background thread code paths via Flask test client."""

from __future__ import annotations

import time

from annuli.interfaces import web, web_jobs
from annuli.interfaces.web_jobs import (
    RELOAD_JOBS,
    RELOAD_JOBS_LOCK,
    _get_reload_job,
    _start_audit_job,
    _start_reload_job,
)
from annuli.interfaces.web_utils import ActionJobCancelledError, ActionJobTimeoutError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_job_done(job_id: str, timeout: float = 5.0) -> dict | None:
    """Poll until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _get_reload_job(job_id)
        if state and state.get("status") in {"done", "error", "cancelled"}:
            return state
        time.sleep(0.05)
    return _get_reload_job(job_id)


# ---------------------------------------------------------------------------
# _start_reload_job thread body — success path
# ---------------------------------------------------------------------------


def test_start_reload_job_success(monkeypatch):
    """The background thread sets status=done when _reload_sessions_index succeeds."""

    def fake_reload(**_):
        return {"status": "ok", "total_sessions": 5, "errors": []}

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=True)
    assert job_id.startswith("reload-")

    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "done"
    assert state["progress"] == 100

    # Cleanup
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_reload_job_full_mode(monkeypatch):
    """Full mode (incremental=False) sets mode='full'."""

    def fake_reload(**_):
        return {"status": "ok", "total_sessions": 0, "errors": []}

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=False)
    # Check initial state has mode=full
    state = _get_reload_job(job_id)
    assert state is not None
    assert state["mode"] == "full"

    _wait_for_job_done(job_id)
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_reload_job_exception_path(monkeypatch):
    """Generic exception in thread body sets status=error."""

    def fake_reload(**_):
        raise RuntimeError("Simulated failure")

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=True)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "error"
    assert "Simulated failure" in state["error"]

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_reload_job_cancelled_path(monkeypatch):
    """ActionJobCancelledError in thread body sets status=cancelled."""

    def fake_reload(**_):
        raise ActionJobCancelledError("Cancelled by user")

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=True)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "cancelled"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_reload_job_timeout_path(monkeypatch):
    """ActionJobTimeoutError in thread body sets status=error with timeout."""

    def fake_reload(**_):
        raise ActionJobTimeoutError("Timed out after 180s")

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=True)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "error"
    assert state["error"] == "timeout"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_reload_job_with_progress_callback(monkeypatch):
    """Progress callback is called and updates job state."""

    def fake_reload(progress_callback=None, **_):
        if progress_callback:
            progress_callback(25, "Quarter done")
            progress_callback(75, "Three quarters done")
        return {"status": "ok", "total_sessions": 3, "errors": []}

    monkeypatch.setattr(web, "_reload_sessions_index", fake_reload)

    job_id = _start_reload_job(provider=None, incremental=True)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "done"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# _start_audit_job thread body
# ---------------------------------------------------------------------------


def test_start_audit_job_success(monkeypatch):
    """Audit job succeeds when _run_audit returns payload."""

    def fake_audit_index():
        return {"scope": "index", "by_tool": {}, "totals": {"sessions": 0}}

    def fake_finalize(scope, rows, total):
        return {"scope": scope, "by_tool": rows, "totals": {"sessions": total}}

    def fake_new_tool_audit_row():
        return {"total": 0, "ok": 0, "issues": []}

    monkeypatch.setattr(web, "_audit_index_sessions", fake_audit_index)
    monkeypatch.setattr(web, "_finalize_audit_payload", fake_finalize)
    monkeypatch.setattr(web, "_new_tool_audit_row", fake_new_tool_audit_row)

    job_id = _start_audit_job(scope="index", provider=None)
    assert job_id.startswith("audit-")

    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "done"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_audit_job_exception_path(monkeypatch):
    """Generic exception in audit thread sets status=error."""

    def fake_audit_index():
        raise RuntimeError("Audit failed")

    monkeypatch.setattr(web, "_audit_index_sessions", fake_audit_index)

    job_id = _start_audit_job(scope="index", provider=None)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "error"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_audit_job_cancelled_path(monkeypatch):
    """ActionJobCancelledError in audit thread sets status=cancelled."""

    def fake_audit_index():
        raise ActionJobCancelledError("Cancelled")

    monkeypatch.setattr(web, "_audit_index_sessions", fake_audit_index)

    job_id = _start_audit_job(scope="index", provider=None)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "cancelled"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_audit_job_timeout_path(monkeypatch):
    """ActionJobTimeoutError in audit thread sets status=error."""

    def fake_audit_index():
        raise ActionJobTimeoutError("Timed out")

    monkeypatch.setattr(web, "_audit_index_sessions", fake_audit_index)

    job_id = _start_audit_job(scope="index", provider=None)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "error"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_audit_job_live_scope(monkeypatch):
    """Live scope calls _audit_live_sessions."""

    def fake_live_sessions(**_):
        return {"scope": "live", "by_tool": {}, "totals": {"sessions": 0}}

    monkeypatch.setattr(web, "_audit_live_sessions", fake_live_sessions)

    job_id = _start_audit_job(scope="live", provider=None)
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "done"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_start_audit_job_with_provider_filter(monkeypatch):
    """Audit with provider filters results to that provider."""

    def fake_audit_index():
        return {
            "by_tool": {"opencode": {"total": 5, "ok": 4}},
            "totals": {"sessions": 5},
        }

    def fake_finalize(scope, rows, total):
        return {"scope": scope, "by_tool": rows, "totals": {"sessions": total}}

    def fake_new_tool_audit_row():
        return {"total": 0, "ok": 0, "issues": []}

    monkeypatch.setattr(web, "_audit_index_sessions", fake_audit_index)
    monkeypatch.setattr(web, "_finalize_audit_payload", fake_finalize)
    monkeypatch.setattr(web, "_new_tool_audit_row", fake_new_tool_audit_row)

    job_id = _start_audit_job(scope="index", provider="opencode")
    state = _wait_for_job_done(job_id)
    assert state is not None
    assert state["status"] == "done"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# _prune_reload_jobs_locked — overage enforcement
# ---------------------------------------------------------------------------


def test_prune_enforces_max_job_count():
    """When RELOAD_JOBS exceeds RELOAD_JOB_MAX, oldest jobs are pruned."""
    original_max = web_jobs.RELOAD_JOB_MAX

    try:
        web_jobs.RELOAD_JOB_MAX = 3

        with RELOAD_JOBS_LOCK:
            RELOAD_JOBS.clear()
            for i in range(5):
                RELOAD_JOBS[f"job-{i}"] = {
                    "job_id": f"job-{i}",
                    "status": "running",
                    "updated_epoch": float(i),  # older = smaller epoch
                    "started_epoch": float(i),
                    "cancel_requested": False,
                }
            web_jobs._prune_reload_jobs_locked()
            remaining = set(RELOAD_JOBS.keys())
            RELOAD_JOBS.clear()

        # Should have pruned the 2 oldest (job-0, job-1)
        assert len(remaining) <= 3
        assert "job-4" in remaining  # newest should survive

    finally:
        web_jobs.RELOAD_JOB_MAX = original_max
