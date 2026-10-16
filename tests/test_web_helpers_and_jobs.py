"""Tests for web_helpers.py and web_jobs.py."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from lore.interfaces.web_helpers import (
    TOOL_STYLES,
    filter_sessions,
    get_style,
    parse_date_param,
    project_label,
)
from lore.interfaces.web_jobs import (
    RELOAD_JOB_TTL_SECONDS,
    RELOAD_JOBS,
    RELOAD_JOBS_LOCK,
    _assert_job_active,
    _cancel_action_job,
    _get_reload_job,
    _prune_reload_jobs_locked,
    _set_reload_job,
)
from lore.interfaces.web_utils import (
    ActionJobCancelledError,
    ActionJobTimeoutError,
    _record_job_outcome,
)

# ---------------------------------------------------------------------------
# web_helpers — get_style
# ---------------------------------------------------------------------------


def test_get_style_known_tool():
    style = get_style("claude-code")
    assert style["color"] == "#C084FC"
    assert style["icon"] == "C"
    assert style["name"] == "Claude"


def test_get_style_unknown_tool():
    style = get_style("some-unknown-tool")
    assert style["color"] == "#A1A1AA"
    assert style["name"] == "some-unknown-tool"


def test_get_style_all_known_tools():
    for tool in TOOL_STYLES:
        s = get_style(tool)
        assert "color" in s
        assert "icon" in s
        assert "name" in s


# ---------------------------------------------------------------------------
# web_helpers — project_label
# ---------------------------------------------------------------------------


def test_project_label_none():
    assert project_label(None) == "Unassigned"


def test_project_label_empty():
    assert project_label("") == "Unassigned"


def test_project_label_path():
    result = project_label("/home/user/projects/lore")
    assert result == "lore"


def test_project_label_just_slash():
    result = project_label("/")
    # Path("/").name is "" so falls back to the path itself
    assert result == "/"


# ---------------------------------------------------------------------------
# web_helpers — parse_date_param
# ---------------------------------------------------------------------------


def test_parse_date_param_empty():
    assert parse_date_param("") is None


def test_parse_date_param_none_string():
    assert parse_date_param(None) is None


def test_parse_date_param_iso_dash():
    result = parse_date_param("2025-06-15")
    assert result is not None
    assert result.year == 2025
    assert result.month == 6
    assert result.day == 15


def test_parse_date_param_iso_slash():
    result = parse_date_param("2025/06/15")
    assert result is not None
    assert result.year == 2025


def test_parse_date_param_iso_datetime():
    result = parse_date_param("2025-06-15T10:30:00")
    assert result is not None
    assert result.hour == 10


def test_parse_date_param_invalid():
    result = parse_date_param("not-a-date")
    assert result is None


def test_parse_date_param_whitespace():
    result = parse_date_param("  2025-06-15  ")
    assert result is not None
    assert result.year == 2025


# ---------------------------------------------------------------------------
# web_helpers — filter_sessions
# ---------------------------------------------------------------------------


def _sessions():
    return [
        {
            "id": "s1",
            "tool": "claude-code",
            "created": "2025-01-15T10:00:00",
            "keywords": ["python", "flask"],
        },
        {
            "id": "s2",
            "tool": "cursor",
            "created": "2025-03-20T12:00:00",
            "keywords": ["bash"],
        },
        {
            "id": "s3",
            "tool": "claude-code",
            "created": "2025-06-01T08:00:00",
            "keywords": ["python", "pytest"],
        },
    ]


def test_filter_sessions_no_filters():
    result = filter_sessions(_sessions())
    assert len(result) == 3


def test_filter_sessions_by_tool():
    result = filter_sessions(_sessions(), tool="claude-code")
    assert len(result) == 2
    assert all(s["tool"] == "claude-code" for s in result)


def test_filter_sessions_by_tag():
    result = filter_sessions(_sessions(), tag="python")
    assert len(result) == 2


def test_filter_sessions_by_tag_no_match():
    result = filter_sessions(_sessions(), tag="rust")
    assert len(result) == 0


def test_filter_sessions_by_start_date():
    start = datetime(2025, 3, 1)
    result = filter_sessions(_sessions(), start=start)
    assert len(result) == 2
    assert all(s["id"] in ("s2", "s3") for s in result)


def test_filter_sessions_by_end_date():
    end = datetime(2025, 2, 1)
    result = filter_sessions(_sessions(), end=end)
    assert len(result) == 1
    assert result[0]["id"] == "s1"


def test_filter_sessions_date_range():
    start = datetime(2025, 2, 1)
    end = datetime(2025, 4, 1)
    result = filter_sessions(_sessions(), start=start, end=end)
    assert len(result) == 1
    assert result[0]["id"] == "s2"


def test_filter_sessions_skips_missing_created():
    sessions = [{"id": "x", "tool": "cursor", "keywords": []}]
    start = datetime(2025, 1, 1)
    result = filter_sessions(sessions, start=start)
    assert len(result) == 0


def test_filter_sessions_skips_invalid_created():
    sessions = [{"id": "x", "tool": "cursor", "created": "INVALID", "keywords": []}]
    start = datetime(2025, 1, 1)
    result = filter_sessions(sessions, start=start)
    assert len(result) == 0


def test_filter_sessions_combined_filters():
    result = filter_sessions(_sessions(), tool="claude-code", tag="pytest")
    assert len(result) == 1
    assert result[0]["id"] == "s3"


# ---------------------------------------------------------------------------
# web_jobs — _prune_reload_jobs_locked
# ---------------------------------------------------------------------------


def _fresh_job(job_id: str, status: str = "done", age_seconds: float = 0.0) -> dict:
    now = time.time() - age_seconds
    return {
        "job_id": job_id,
        "status": status,
        "updated_epoch": now,
        "started_epoch": now,
        "cancel_requested": False,
    }


def test_prune_stale_done_jobs():
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.clear()
        old_ttl = RELOAD_JOB_TTL_SECONDS
        # Add a job that is older than TTL
        RELOAD_JOBS["old-job"] = _fresh_job("old-job", status="done", age_seconds=old_ttl + 100)
        RELOAD_JOBS["new-job"] = _fresh_job("new-job", status="done", age_seconds=10)
        _prune_reload_jobs_locked()
        remaining = set(RELOAD_JOBS.keys())
        RELOAD_JOBS.clear()

    assert "old-job" not in remaining
    assert "new-job" in remaining


def test_prune_running_jobs_not_pruned():
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.clear()
        RELOAD_JOBS["running-job"] = _fresh_job("running-job", status="running", age_seconds=10000)
        _prune_reload_jobs_locked()
        remaining = set(RELOAD_JOBS.keys())
        RELOAD_JOBS.clear()

    assert "running-job" in remaining


# ---------------------------------------------------------------------------
# web_jobs — _get_reload_job and _set_reload_job
# ---------------------------------------------------------------------------


def test_get_reload_job_missing():
    result = _get_reload_job("nonexistent-job-xyz")
    assert result is None


def test_set_and_get_reload_job():
    job_id = "test-job-abc123"
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS[job_id] = _fresh_job(job_id, status="running")

    _set_reload_job(job_id, status="done", progress=100, message="Finished")

    state = _get_reload_job(job_id)
    assert state is not None
    assert state["status"] == "done"
    assert state["progress"] == 100
    assert state["message"] == "Finished"

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_set_reload_job_noop_when_missing():
    # Should not raise even if job doesn't exist
    _set_reload_job("ghost-job-xyz", status="done")  # no exception expected


# ---------------------------------------------------------------------------
# web_jobs — _cancel_action_job
# ---------------------------------------------------------------------------


def test_cancel_job_missing():
    result = _cancel_action_job("ghost-cancel-xyz")
    assert result is False


def test_cancel_job_running():
    job_id = "cancel-running-test"
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS[job_id] = _fresh_job(job_id, status="running")

    result = _cancel_action_job(job_id)
    assert result is True

    state = _get_reload_job(job_id)
    assert state is not None
    assert state["cancel_requested"] is True

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_cancel_job_already_done():
    job_id = "cancel-done-test"
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS[job_id] = _fresh_job(job_id, status="done")

    result = _cancel_action_job(job_id)
    # done jobs: cancel returns True but doesn't set cancel_requested
    assert result is True

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# web_jobs — _assert_job_active
# ---------------------------------------------------------------------------


def test_assert_job_active_missing_raises():
    with pytest.raises(ActionJobCancelledError, match="no longer exists"):
        _assert_job_active("no-such-job-xyz")


def test_assert_job_active_cancel_requested():
    job_id = "assert-cancel-test"
    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS[job_id] = {
            **_fresh_job(job_id, status="running"),
            "cancel_requested": True,
        }

    with pytest.raises(ActionJobCancelledError, match="Cancelled"):
        _assert_job_active(job_id)

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


def test_assert_job_active_timed_out():
    from lore.interfaces import web_jobs

    job_id = "assert-timeout-test"
    with RELOAD_JOBS_LOCK:
        job = _fresh_job(job_id, status="running")
        job["started_epoch"] = time.time() - 99999  # Way past timeout
        job["cancel_requested"] = False
        RELOAD_JOBS[job_id] = job

    old_timeout = web_jobs.ACTION_JOB_TIMEOUT_SECONDS
    web_jobs.ACTION_JOB_TIMEOUT_SECONDS = 1  # 1 second timeout

    try:
        with pytest.raises(ActionJobTimeoutError):
            _assert_job_active(job_id)
    finally:
        web_jobs.ACTION_JOB_TIMEOUT_SECONDS = old_timeout
        with RELOAD_JOBS_LOCK:
            RELOAD_JOBS.pop(job_id, None)


def test_assert_job_active_ok():
    job_id = "assert-ok-test"
    with RELOAD_JOBS_LOCK:
        job = _fresh_job(job_id, status="running")
        job["cancel_requested"] = False
        job["started_epoch"] = time.time()  # Just started
        RELOAD_JOBS[job_id] = job

    # Should not raise
    _assert_job_active(job_id)

    with RELOAD_JOBS_LOCK:
        RELOAD_JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# _record_job_outcome — all branches
# ---------------------------------------------------------------------------


def test_record_job_outcome_completed():
    _record_job_outcome("reload", "completed", time.time() - 1.0)
    _record_job_outcome("audit", "completed", time.time() - 0.5)


def test_record_job_outcome_cancelled():
    _record_job_outcome("reload", "cancelled", time.time() - 1.0)
    _record_job_outcome("audit", "cancelled", time.time() - 0.5)


def test_record_job_outcome_failed():
    _record_job_outcome("reload", "failed", time.time() - 1.0)
    _record_job_outcome("audit", "failed", time.time() - 0.5)
