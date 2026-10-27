"""Durable, resumable analysis jobs (#179).

An analysis job used to exist only as an in-process dict entry, so a restart
lost the request identity, the progress and any partial result. This service
makes the v2 store the source of truth: a job row records *what* was asked
(provider, model, prompt version, cancellation intent), *what it read* (source
revision / session id) and *how far it got* (state plus chunk rows).

Two invariants drive the design:

* **A new run never replaces an earlier run.** Every run is its own job row, and
  chunk results are keyed by ``(job_id, chunk_key)``. Starting a second run
  creates new rows; nothing is deleted or overwritten.
* **Completing a chunk is idempotent.** The first recorded result of a chunk
  wins — a retry or a duplicate callback returns ``False`` and changes nothing.
  A *failed* chunk can still be completed by a later attempt (the ``attempt``
  counter makes the retry visible).

State vocabularies are closed sets so a caller cannot invent a state that the
web UI or the CLI does not understand. ``cancelled``, ``timed_out`` and
``failed`` are deliberately distinct: a cancellation is a user decision, a
timeout is a wall-clock budget, and a failure is the provider or the work
itself. Retry is visible through ``analysis_job_chunks.attempt``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: Job states. ``pending`` = created but no chunk claimed yet; the rest are
#: terminal except ``running``.
JOB_STATES: Tuple[str, ...] = (
    "pending",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
)

#: Chunk states. ``complete`` and ``failed`` are terminal until a resume
#: requeues a failed chunk; ``running`` means a worker claimed it.
CHUNK_STATES: Tuple[str, ...] = ("pending", "running", "complete", "failed")

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "timed_out", "cancelled"})

#: States a resume refuses to reopen: the work already finished or was stopped
#: deliberately by the user.
_NOT_RESUMABLE = frozenset({"completed", "cancelled"})


class AnalysisJobError(RuntimeError):
    """A job or chunk operation was requested that the store cannot honour."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block in an immediate transaction (or join the caller's)."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


@dataclass(frozen=True)
class AnalysisChunk:
    """One unit of resumable work inside a job."""

    job_id: str
    chunk_key: str
    status: str
    attempt: int
    result: Any
    error: Optional[str]
    updated: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "chunk_key": self.chunk_key,
            "status": self.status,
            "attempt": self.attempt,
            "result": self.result,
            "error": self.error,
            "updated": self.updated,
        }


@dataclass(frozen=True)
class AnalysisJob:
    """A persisted analysis job plus its derived progress and chunk rows."""

    id: str
    kind: str
    request: Dict[str, Any]
    session_id: Optional[str]
    source_revision: Optional[str]
    state: str
    progress: Dict[str, Any]
    error: Optional[str]
    created: str
    updated: str
    chunks: Tuple[AnalysisChunk, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "request": self.request,
            "session_id": self.session_id,
            "source_revision": self.source_revision,
            "state": self.state,
            "progress": self.progress,
            "error": self.error,
            "created": self.created,
            "updated": self.updated,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }


def _json_or_none(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _chunk_from_row(row: sqlite3.Row) -> AnalysisChunk:
    return AnalysisChunk(
        job_id=str(row["job_id"]),
        chunk_key=str(row["chunk_key"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result=_json_or_none(row["result_json"]),
        error=row["error"],
        updated=str(row["updated"]),
    )


def _chunks_for(conn: sqlite3.Connection, job_id: str) -> Tuple[AnalysisChunk, ...]:
    rows = conn.execute(
        "SELECT job_id, chunk_key, status, attempt, result_json, error, updated "
        "FROM analysis_job_chunks WHERE job_id = ? ORDER BY chunk_key",
        (job_id,),
    ).fetchall()
    return tuple(_chunk_from_row(row) for row in rows)


def _chunk_progress(counts: Dict[str, int]) -> Dict[str, Any]:
    """Counts and percentage for a job's chunks; zero chunks → no percent."""
    total = sum(counts.values())
    progress: Dict[str, Any] = {
        "total": total,
        "complete": counts.get("complete", 0),
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
    }
    progress["percent"] = int(round(100 * counts.get("complete", 0) / total)) if total else 0
    return progress


def _counts_of(chunks: Sequence[AnalysisChunk]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.status] = counts.get(chunk.status, 0) + 1
    return counts


def _merged_progress(stored: Dict[str, Any], counts: Dict[str, int]) -> Dict[str, Any]:
    """Stored progress (message, etc.) with live chunk counters on top."""
    merged = dict(stored)
    if counts:
        merged.update(_chunk_progress(counts))
    return merged


def _job_from_row(
    row: sqlite3.Row,
    *,
    chunks: Tuple[AnalysisChunk, ...] = (),
    counts: Optional[Dict[str, int]] = None,
) -> AnalysisJob:
    stored_progress = _json_or_none(row["progress_json"])
    request = _json_or_none(row["request_json"])
    return AnalysisJob(
        id=str(row["id"]),
        kind=str(row["kind"]),
        request=request if isinstance(request, dict) else {},
        session_id=row["session_id"],
        source_revision=row["source_revision"],
        state=str(row["state"]),
        progress=_merged_progress(
            stored_progress if isinstance(stored_progress, dict) else {},
            counts if counts is not None else _counts_of(chunks),
        ),
        error=row["error"],
        created=str(row["created"]),
        updated=str(row["updated"]),
        chunks=chunks,
    )


def create_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    request: Optional[Dict[str, Any]] = None,
    chunk_keys: Sequence[str] = (),
    session_id: Optional[str] = None,
    source_revision: Optional[str] = None,
    state: str = "running",
    job_id: Optional[str] = None,
) -> AnalysisJob:
    """Create a job with one pending row per ``chunk_keys`` entry.

    ``request`` records the provider/model/prompt version and any cancellation
    intent; it is stored verbatim. A duplicate ``job_id`` raises
    :class:`sqlite3.IntegrityError` rather than replacing the existing run — a
    new run must never overwrite an earlier one.
    """
    if state not in JOB_STATES:
        raise AnalysisJobError(f"unknown job state {state!r}")
    job_key = job_id or f"{kind}-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _transaction(conn):
        conn.execute(
            "INSERT INTO analysis_jobs "
            "(id, kind, request_json, session_id, source_revision, state, progress_json, "
            " error, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                job_key,
                kind,
                json.dumps(request or {}, sort_keys=True, ensure_ascii=False),
                session_id,
                source_revision,
                state,
                json.dumps({}),
                now,
                now,
            ),
        )
        for chunk_key in chunk_keys:
            conn.execute(
                "INSERT INTO analysis_job_chunks "
                "(job_id, chunk_key, status, attempt, result_json, error, updated) "
                "VALUES (?, ?, 'pending', 0, NULL, NULL, ?)",
                (job_key, chunk_key, now),
            )
    return load_job(conn, job_key)


def load_job(conn: sqlite3.Connection, job_id: str) -> Optional[AnalysisJob]:
    """Load one job with its chunks, or ``None`` when it does not exist."""
    row = conn.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return _job_from_row(row, chunks=_chunks_for(conn, job_id))


def list_jobs(
    conn: sqlite3.Connection,
    *,
    kind: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 50,
) -> List[AnalysisJob]:
    """List jobs newest first, optionally filtered by ``kind`` or ``state``."""
    if state is not None and state not in JOB_STATES:
        raise AnalysisJobError(f"unknown job state {state!r}")
    clauses: List[str] = []
    params: List[Any] = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if state:
        clauses.append("state = ?")
        params.append(state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM analysis_jobs{where} ORDER BY created DESC, id DESC LIMIT ?",
        (*params, max(1, int(limit))),
    ).fetchall()
    # One grouped query for chunk counters instead of per-job queries.
    counts: Dict[str, Dict[str, int]] = {}
    if rows:
        placeholders = ",".join("?" for _ in rows)
        for count_row in conn.execute(
            "SELECT job_id, status, COUNT(*) AS n FROM analysis_job_chunks "
            f"WHERE job_id IN ({placeholders}) GROUP BY job_id, status",
            tuple(row["id"] for row in rows),
        ):
            bucket = counts.setdefault(str(count_row["job_id"]), {})
            bucket[str(count_row["status"])] = int(count_row["n"])
    return [_job_from_row(row, counts=counts.get(str(row["id"]), {})) for row in rows]


def claim_next_pending_chunk(conn: sqlite3.Connection, job_id: str) -> Optional[AnalysisChunk]:
    """Claim the next pending chunk, marking it ``running`` and bumping attempt.

    Returns ``None`` when the job has no pending chunk left or is in a terminal
    state (a cancelled job must not be picked up by a worker). The claim is one
    immediate transaction, so two workers cannot claim the same chunk.
    """
    with _transaction(conn):
        job = conn.execute("SELECT state FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise AnalysisJobError(f"unknown analysis job {job_id!r}")
        if str(job["state"]) in TERMINAL_JOB_STATES:
            return None
        row = conn.execute(
            "SELECT job_id, chunk_key FROM analysis_job_chunks "
            "WHERE job_id = ? AND status = 'pending' ORDER BY chunk_key LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        now = _now()
        conn.execute(
            "UPDATE analysis_job_chunks SET status = 'running', attempt = attempt + 1, updated = ? "
            "WHERE job_id = ? AND chunk_key = ?",
            (now, job_id, row["chunk_key"]),
        )
        conn.execute(
            "UPDATE analysis_jobs SET state = 'running', updated = ? WHERE id = ?",
            (now, job_id),
        )
    claimed = conn.execute(
        "SELECT job_id, chunk_key, status, attempt, result_json, error, updated "
        "FROM analysis_job_chunks WHERE job_id = ? AND chunk_key = ?",
        (job_id, row["chunk_key"]),
    ).fetchone()
    return _chunk_from_row(claimed)


def record_chunk_result(
    conn: sqlite3.Connection,
    job_id: str,
    chunk_key: str,
    *,
    result: Any = None,
    status: str = "complete",
    error: Optional[str] = None,
) -> bool:
    """Record one chunk outcome. Returns True when the row changed.

    Idempotent: the first ``complete`` result wins, and repeating a ``failed``
    record is a no-op. A failed chunk can later be completed (the retry is
    visible through ``attempt``). Job state advances to ``completed`` when every
    chunk is complete, and to ``failed`` when every chunk is terminal and at
    least one failed — a cancelled job keeps its cancelled state.
    """
    if status not in {"complete", "failed"}:
        raise AnalysisJobError(f"chunk results must be complete or failed, got {status!r}")
    with _transaction(conn):
        row = conn.execute(
            "SELECT status FROM analysis_job_chunks WHERE job_id = ? AND chunk_key = ?",
            (job_id, chunk_key),
        ).fetchone()
        if row is None:
            raise AnalysisJobError(f"unknown chunk {chunk_key!r} on job {job_id!r}")
        previous = str(row["status"])
        if previous == "complete":
            return False
        if previous == "failed" and status == "failed":
            return False
        now = _now()
        conn.execute(
            "UPDATE analysis_job_chunks "
            "SET status = ?, result_json = ?, error = ?, updated = ? "
            "WHERE job_id = ? AND chunk_key = ?",
            (
                status,
                json.dumps(result, sort_keys=True, ensure_ascii=False)
                if result is not None
                else None,
                error,
                now,
                job_id,
                chunk_key,
            ),
        )
        _advance_job_state(conn, job_id, now)
    return True


def _advance_job_state(conn: sqlite3.Connection, job_id: str, now: str) -> None:
    """Move the job to a terminal state once every chunk is terminal."""
    job = conn.execute("SELECT state FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or str(job["state"]) in _NOT_RESUMABLE:
        return
    counts = {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM analysis_job_chunks "
            "WHERE job_id = ? GROUP BY status",
            (job_id,),
        )
    }
    total = sum(counts.values())
    complete = counts.get("complete", 0)
    failed = counts.get("failed", 0)
    if total and complete == total:
        conn.execute(
            "UPDATE analysis_jobs SET state = 'completed', error = NULL, updated = ? WHERE id = ?",
            (now, job_id),
        )
    elif total and complete + failed == total and failed:
        conn.execute(
            "UPDATE analysis_jobs SET state = 'failed', updated = ? WHERE id = ?",
            (now, job_id),
        )


def resume_job(conn: sqlite3.Connection, job_id: str) -> AnalysisJob:
    """Requeue unfinished chunks of an interrupted job and return it.

    Chunks already ``complete`` are skipped — they keep their result and are
    never re-run. ``running`` chunks (claimed by a worker that died) and
    ``failed`` chunks (retryable) go back to ``pending``; the attempt counter
    keeps the retry history. A completed job is returned unchanged, and a
    cancelled job stays cancelled: resume must not undo a user decision.
    """
    with _transaction(conn):
        job = conn.execute("SELECT state FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise AnalysisJobError(f"unknown analysis job {job_id!r}")
        if str(job["state"]) in _NOT_RESUMABLE:
            return load_job(conn, job_id)  # type: ignore[return-value]
        now = _now()
        conn.execute(
            "UPDATE analysis_job_chunks SET status = 'pending', updated = ? "
            "WHERE job_id = ? AND status IN ('running', 'failed')",
            (now, job_id),
        )
        conn.execute(
            "UPDATE analysis_jobs SET state = 'running', error = NULL, updated = ? WHERE id = ?",
            (now, job_id),
        )
    loaded = load_job(conn, job_id)
    assert loaded is not None  # the row was just read inside the transaction
    return loaded


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    state: Optional[str] = None,
    progress: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> AnalysisJob:
    """Merge identity/progress updates into a job (web jobs use this).

    ``progress`` is merged, not replaced, so a caller can report a message
    without dropping the chunk counters. Jobs with no chunks (the web reload,
    audit and summary jobs) rely on the stored percent/message values.
    """
    if state is not None and state not in JOB_STATES:
        raise AnalysisJobError(f"unknown job state {state!r}")
    with _transaction(conn):
        row = conn.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise AnalysisJobError(f"unknown analysis job {job_id!r}")
        stored = _json_or_none(row["progress_json"])
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged.update(progress or {})
        now = _now()
        conn.execute(
            "UPDATE analysis_jobs SET state = ?, progress_json = ?, error = ?, updated = ? "
            "WHERE id = ?",
            (
                state if state is not None else row["state"],
                json.dumps(merged, sort_keys=True, ensure_ascii=False),
                error if error is not None else row["error"],
                now,
                job_id,
            ),
        )
    loaded = load_job(conn, job_id)
    assert loaded is not None
    return loaded


def cancel_job(conn: sqlite3.Connection, job_id: str) -> AnalysisJob:
    """Request cancellation: the job becomes ``cancelled`` and stops being claimable.

    Finished chunks keep their results; unfinished chunks stay unfinished so a
    later audit can see what was attempted. Cancelling an already completed job
    is a no-op. A resume does not reopen a cancelled job.
    """
    with _transaction(conn):
        row = conn.execute("SELECT state FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise AnalysisJobError(f"unknown analysis job {job_id!r}")
        if str(row["state"]) != "completed":
            conn.execute(
                "UPDATE analysis_jobs SET state = 'cancelled', error = 'cancelled', updated = ? "
                "WHERE id = ?",
                (_now(), job_id),
            )
    loaded = load_job(conn, job_id)
    assert loaded is not None
    return loaded


__all__ = [
    "AnalysisChunk",
    "AnalysisJob",
    "AnalysisJobError",
    "CHUNK_STATES",
    "JOB_STATES",
    "TERMINAL_JOB_STATES",
    "cancel_job",
    "claim_next_pending_chunk",
    "create_job",
    "list_jobs",
    "load_job",
    "record_chunk_result",
    "resume_job",
    "update_job",
]
