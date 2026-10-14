"""Job management for background reload and audit operations.

This module handles asynchronous job execution for session reloading
and auditing operations in the web interface.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .web_utils import (
    ActionJobCancelledError,
    ActionJobTimeoutError,
    _metrics_inc,
    _record_job_outcome,
)

logger = logging.getLogger(__name__)

# Job state - shared across all requests
RELOAD_JOBS: dict[str, dict[str, Any]] = {}
RELOAD_JOBS_LOCK = threading.Lock()

# Configuration from environment
RELOAD_JOB_TTL_SECONDS = max(
    60, int(os.environ.get("LORE_ACTION_JOB_TTL_SECONDS", "3600") or "3600")
)
RELOAD_JOB_MAX = max(32, int(os.environ.get("LORE_ACTION_JOB_MAX", "256") or "256"))
ACTION_JOB_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("LORE_ACTION_JOB_TIMEOUT_SECONDS", "180") or "180")
)
APP_STARTED_EPOCH = time.time()


def _prune_reload_jobs_locked(now_epoch: Optional[float] = None) -> None:
    """Remove stale jobs and enforce max job count. Must be called with lock held."""
    now = now_epoch if now_epoch is not None else time.time()
    stale_ids = []
    for job_id, state in RELOAD_JOBS.items():
        status = str(state.get("status") or "")
        updated_epoch = float(state.get("updated_epoch") or 0)
        if status in {"done", "error"} and updated_epoch > 0:
            if now - updated_epoch > RELOAD_JOB_TTL_SECONDS:
                stale_ids.append(job_id)

    for job_id in stale_ids:
        RELOAD_JOBS.pop(job_id, None)

    if len(RELOAD_JOBS) <= RELOAD_JOB_MAX:
        return

    overage = len(RELOAD_JOBS) - RELOAD_JOB_MAX
    by_age = sorted(
        RELOAD_JOBS.items(),
        key=lambda item: float(item[1].get("updated_epoch") or 0),
    )
    for job_id, _ in by_age[:overage]:
        RELOAD_JOBS.pop(job_id, None)


def _set_reload_job(job_id: str, **updates: Any) -> None:
    """Update job state with pruning."""
    with RELOAD_JOBS_LOCK:
        state = RELOAD_JOBS.get(job_id)
        if not state:
            return
        now_epoch = time.time()
        state.update(updates)
        state["updated_epoch"] = now_epoch
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _prune_reload_jobs_locked(now_epoch=now_epoch)


def _assert_job_active(job_id: str) -> None:
    """Check if job is still active and not cancelled/timed out."""
    state = _get_reload_job(job_id)
    if not state:
        raise ActionJobCancelledError("Action no longer exists")

    if bool(state.get("cancel_requested")):
        _set_reload_job(
            job_id,
            status="cancelled",
            message="Cancelled by user",
            error="cancelled",
        )
        raise ActionJobCancelledError("Cancelled by user")

    started_epoch = float(state.get("started_epoch") or 0)
    if started_epoch > 0 and (time.time() - started_epoch) > ACTION_JOB_TIMEOUT_SECONDS:
        timeout_message = f"Timed out after {ACTION_JOB_TIMEOUT_SECONDS}s"
        _set_reload_job(
            job_id,
            status="error",
            message=timeout_message,
            error="timeout",
        )
        raise ActionJobTimeoutError(timeout_message)


def _cancel_action_job(job_id: str) -> bool:
    """Request cancellation of a running job."""
    with RELOAD_JOBS_LOCK:
        state = RELOAD_JOBS.get(job_id)
        if not state:
            return False
        if str(state.get("status") or "") not in {"running"}:
            return True
        state["cancel_requested"] = True
        state["updated_epoch"] = time.time()
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return True


def _get_reload_job(job_id: str) -> Optional[dict]:
    """Get a copy of job state."""
    with RELOAD_JOBS_LOCK:
        _prune_reload_jobs_locked()
        state = RELOAD_JOBS.get(job_id)
        if not state:
            return None
        return dict(state)


def _start_reload_job(provider: Optional[str], incremental: bool = True) -> str:
    """Start a background reload job."""
    _metrics_inc("reload_jobs_started")
    job_id = f"reload-{uuid.uuid4().hex[:12]}"
    now_epoch = time.time()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with RELOAD_JOBS_LOCK:
        _prune_reload_jobs_locked(now_epoch=now_epoch)
        RELOAD_JOBS[job_id] = {
            "job_id": job_id,
            "kind": "reload",
            "provider": provider or "",
            "mode": "incremental" if incremental else "full",
            "status": "running",
            "progress": 0,
            "message": "Queued",
            "started_epoch": now_epoch,
            "updated_epoch": now_epoch,
            "started_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }

    def run() -> None:
        # Late import to avoid circular dependency
        from .web import _reload_sessions_index

        try:

            def progress_callback(progress: int, message: str) -> None:
                _assert_job_active(job_id)
                _set_reload_job(job_id, progress=max(0, min(100, int(progress))), message=message)

            def should_stop() -> bool:
                _assert_job_active(job_id)
                return False

            payload = _reload_sessions_index(
                provider=provider,
                progress_callback=progress_callback,
                should_stop=should_stop,
                incremental=incremental,
            )
            errors = list(payload.get("errors") or []) if isinstance(payload, dict) else []
            _set_reload_job(
                job_id,
                status="done",
                progress=100,
                message="Completed",
                result=payload,
                errors=errors,
            )
            _record_job_outcome("reload", "completed", now_epoch)
        except ActionJobCancelledError:
            _metrics_inc("action_jobs_cancelled")
            _record_job_outcome("reload", "cancelled", now_epoch)
            _set_reload_job(
                job_id,
                status="cancelled",
                message="Cancelled by user",
                error="cancelled",
            )
        except ActionJobTimeoutError as exc:
            _metrics_inc("action_jobs_timed_out")
            _record_job_outcome("reload", "failed", now_epoch)
            _set_reload_job(
                job_id,
                status="error",
                message=str(exc),
                error="timeout",
            )
        except Exception as exc:
            _record_job_outcome("reload", "failed", now_epoch)
            _set_reload_job(job_id, status="error", message=str(exc), error=str(exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return job_id


def _run_audit(
    scope: str,
    provider: Optional[str],
    progress_callback: Optional[Callable[[int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run audit synchronously (called from background thread)."""
    # Late import to avoid circular dependency
    from .web import (
        _audit_index_sessions,
        _audit_live_sessions,
        _finalize_audit_payload,
        _new_tool_audit_row,
    )

    if scope == "live":
        if progress_callback:
            progress_callback(10, "Collecting live sessions")
        payload = _audit_live_sessions(provider=provider, should_stop=should_stop)
        if progress_callback:
            progress_callback(100, "Completed")
        return payload

    if progress_callback:
        progress_callback(10, "Loading index sessions")
    if should_stop and should_stop():
        raise ActionJobCancelledError("Cancelled by user")
    payload = _audit_index_sessions()
    if provider:
        rows = payload.get("by_tool", {})
        filtered = {provider: rows.get(provider, _new_tool_audit_row())}
        total = int(filtered.get(provider, {}).get("total", 0))
        payload = _finalize_audit_payload("index", filtered, total)
    if progress_callback:
        progress_callback(100, "Completed")
    return payload


def _start_audit_job(scope: str, provider: Optional[str]) -> str:
    """Start a background audit job."""
    _metrics_inc("audit_jobs_started")
    job_id = f"audit-{uuid.uuid4().hex[:12]}"
    now_epoch = time.time()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with RELOAD_JOBS_LOCK:
        _prune_reload_jobs_locked(now_epoch=now_epoch)
        RELOAD_JOBS[job_id] = {
            "job_id": job_id,
            "kind": "audit",
            "provider": provider or "",
            "scope": scope,
            "status": "running",
            "progress": 0,
            "message": "Queued",
            "started_epoch": now_epoch,
            "updated_epoch": now_epoch,
            "started_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }

    def run() -> None:
        try:

            def progress_callback(progress: int, message: str) -> None:
                _assert_job_active(job_id)
                _set_reload_job(job_id, progress=max(0, min(100, int(progress))), message=message)

            def should_stop() -> bool:
                _assert_job_active(job_id)
                return False

            payload = _run_audit(
                scope,
                provider,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
            _set_reload_job(
                job_id, status="done", progress=100, message="Completed", result=payload
            )
            _record_job_outcome("audit", "completed", now_epoch)
        except ActionJobCancelledError:
            _metrics_inc("action_jobs_cancelled")
            _record_job_outcome("audit", "cancelled", now_epoch)
            _set_reload_job(
                job_id,
                status="cancelled",
                message="Cancelled by user",
                error="cancelled",
            )
        except ActionJobTimeoutError as exc:
            _metrics_inc("action_jobs_timed_out")
            _record_job_outcome("audit", "failed", now_epoch)
            _set_reload_job(
                job_id,
                status="error",
                message=str(exc),
                error="timeout",
            )
        except Exception as exc:
            _record_job_outcome("audit", "failed", now_epoch)
            _set_reload_job(job_id, status="error", message=str(exc), error=str(exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return job_id


def _running_summary_job_for_session(session_id: str) -> Optional[str]:
    with RELOAD_JOBS_LOCK:
        for job_id, state in RELOAD_JOBS.items():
            if (
                state.get("kind") == "session_summary"
                and state.get("session_id") == session_id
                and str(state.get("status") or "") == "running"
            ):
                return job_id
    return None


def _start_session_summary_job(session_id: str) -> str:
    """Start a background job that generates one session summary (local provider only).

    The "already running?" check and the insert happen under one lock hold, so two
    overlapping requests can never start two jobs for the same session.
    """
    job_id = f"summary-{uuid.uuid4().hex[:12]}"
    now_epoch = time.time()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with RELOAD_JOBS_LOCK:
        for other_id, state in RELOAD_JOBS.items():
            if (
                state.get("kind") == "session_summary"
                and state.get("session_id") == session_id
                and str(state.get("status") or "") == "running"
            ):
                return other_id
        _prune_reload_jobs_locked(now_epoch=now_epoch)
        RELOAD_JOBS[job_id] = {
            "job_id": job_id,
            "kind": "session_summary",
            "session_id": session_id,
            "status": "running",
            "progress": 0,
            "message": "Queued",
            "started_epoch": now_epoch,
            "updated_epoch": now_epoch,
            "started_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }

    _metrics_inc("summary_jobs_started")

    def run() -> None:
        from lore.services.summarize import summarize_session as run_summarize_session
        from lore.services.summarize_cli import build_mcp_summarize_provider, web_summarize_enabled

        from .web_data import OUTPUT_DIR

        try:

            def progress_callback(progress: int, message: str) -> None:
                _assert_job_active(job_id)
                _set_reload_job(job_id, progress=max(0, min(100, int(progress))), message=message)

            def should_stop() -> bool:
                _assert_job_active(job_id)
                return False

            progress_callback(5, "Preparing local provider")
            if not web_summarize_enabled():
                raise ValueError("Local summary provider is not configured")
            provider, model_label = build_mcp_summarize_provider()
            progress_callback(15, "Summarizing session")
            outcome = run_summarize_session(
                provider,
                OUTPUT_DIR,
                session_id,
                model_label=model_label,
                force=True,
            )
            if should_stop():
                return
            _set_reload_job(
                job_id,
                status="done",
                progress=100,
                message="Completed",
                result={"outcome": outcome, "session_id": session_id},
            )
            _record_job_outcome("session_summary", "completed", now_epoch)
        except ActionJobCancelledError:
            _metrics_inc("action_jobs_cancelled")
            _record_job_outcome("session_summary", "cancelled", now_epoch)
            _set_reload_job(
                job_id,
                status="cancelled",
                message="Cancelled by user",
                error="cancelled",
            )
        except ActionJobTimeoutError as exc:
            _metrics_inc("action_jobs_timed_out")
            _record_job_outcome("session_summary", "failed", now_epoch)
            _set_reload_job(
                job_id,
                status="error",
                message=str(exc),
                error="timeout",
            )
        except Exception as exc:
            _record_job_outcome("session_summary", "failed", now_epoch)
            # Clients get a fixed message: exception text may carry paths or prompt text.
            logger.warning("Summary job failed for %s: %s", session_id, type(exc).__name__)
            _set_reload_job(
                job_id,
                status="error",
                message="Summary could not be generated",
                error="summary_failed",
            )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return job_id


__all__ = [
    "RELOAD_JOBS",
    "RELOAD_JOBS_LOCK",
    "RELOAD_JOB_TTL_SECONDS",
    "RELOAD_JOB_MAX",
    "ACTION_JOB_TIMEOUT_SECONDS",
    "APP_STARTED_EPOCH",
    "_prune_reload_jobs_locked",
    "_set_reload_job",
    "_assert_job_active",
    "_cancel_action_job",
    "_get_reload_job",
    "_start_reload_job",
    "_run_audit",
    "_start_audit_job",
    "_running_summary_job_for_session",
    "_start_session_summary_job",
]
