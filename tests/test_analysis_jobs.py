"""Durable, resumable analysis jobs (#179).

The contract under test: a job persists its request identity and chunk
progress, an interrupted run resumes only the unfinished chunks, completing a
chunk twice changes nothing, and a *new* run never deletes or overwrites the
rows of an earlier run.
"""

from __future__ import annotations

import pytest

from lore.services.analysis_jobs import (
    CHUNK_STATES,
    JOB_STATES,
    AnalysisJobError,
    cancel_job,
    claim_next_pending_chunk,
    create_job,
    list_jobs,
    load_job,
    record_chunk_result,
    resume_job,
    update_job,
)
from lore.storage.schema import apply_migrations, current_version, open_connection


@pytest.fixture()
def conn(tmp_path):
    connection = open_connection(tmp_path / "index_v2.sqlite")
    apply_migrations(connection)
    yield connection
    connection.close()


def _job(conn, **kwargs):
    defaults = dict(
        kind="session_summary",
        request={"provider": "ollama", "model": "qwen", "prompt_version": "v1"},
        chunk_keys=["chunk-1", "chunk-2"],
        session_id="session-a",
        source_revision="rev-1",
    )
    defaults.update(kwargs)
    return create_job(conn, **defaults)


# ---------------------------------------------------------------------------
# Create / load / list
# ---------------------------------------------------------------------------


def test_create_persists_identity_and_pending_chunks(conn):
    job = _job(conn)

    assert job.id.startswith("session_summary-")
    assert job.state == "running"
    assert job.request == {"model": "qwen", "prompt_version": "v1", "provider": "ollama"}
    assert job.session_id == "session-a"
    assert job.source_revision == "rev-1"
    assert [chunk.chunk_key for chunk in job.chunks] == ["chunk-1", "chunk-2"]
    assert {chunk.status for chunk in job.chunks} == {"pending"}
    assert job.progress["total"] == 2
    assert job.progress["pending"] == 2
    assert job.progress["percent"] == 0

    loaded = load_job(conn, job.id)
    assert loaded is not None
    assert loaded.to_dict() == job.to_dict()


def test_list_jobs_filters_and_orders_newest_first(conn):
    first = _job(conn, chunk_keys=["a"])
    second = _job(conn, kind="audit", chunk_keys=["b"])
    conn.execute(
        "UPDATE analysis_jobs SET created = '2026-01-01T00:00:00Z' WHERE id = ?", (first.id,)
    )
    conn.execute(
        "UPDATE analysis_jobs SET created = '2026-01-02T00:00:00Z' WHERE id = ?", (second.id,)
    )

    assert [job.id for job in list_jobs(conn)] == [second.id, first.id]
    assert [job.id for job in list_jobs(conn, kind="audit")] == [second.id]
    assert [job.id for job in list_jobs(conn, state="running")] == [second.id, first.id]


def test_unknown_chunk_and_job_raise(conn):
    job = _job(conn)
    with pytest.raises(AnalysisJobError):
        record_chunk_result(conn, job.id, "not-a-chunk", result={})
    with pytest.raises(AnalysisJobError):
        claim_next_pending_chunk(conn, "missing-job")
    with pytest.raises(AnalysisJobError):
        list_jobs(conn, state="not-a-state")


# ---------------------------------------------------------------------------
# Claim / complete / idempotency
# ---------------------------------------------------------------------------


def test_claim_marks_running_and_counts_attempts(conn):
    job = _job(conn)

    claimed = claim_next_pending_chunk(conn, job.id)
    assert claimed is not None
    assert claimed.chunk_key == "chunk-1"
    assert claimed.status == "running"
    assert claimed.attempt == 1

    again = claim_next_pending_chunk(conn, job.id)
    assert again is not None and again.chunk_key == "chunk-2"

    assert claim_next_pending_chunk(conn, job.id) is None


def test_completing_every_chunk_completes_the_job(conn):
    job = _job(conn)

    claimed = claim_next_pending_chunk(conn, job.id)
    assert record_chunk_result(conn, job.id, claimed.chunk_key, result={"text": "one"})
    claimed = claim_next_pending_chunk(conn, job.id)
    assert record_chunk_result(conn, job.id, claimed.chunk_key, result={"text": "two"})

    loaded = load_job(conn, job.id)
    assert loaded is not None
    assert loaded.state == "completed"
    assert loaded.progress["complete"] == 2
    assert loaded.progress["percent"] == 100
    assert [chunk.result for chunk in loaded.chunks] == [{"text": "one"}, {"text": "two"}]


def test_recording_a_completed_chunk_twice_is_idempotent(conn):
    job = _job(conn)
    claimed = claim_next_pending_chunk(conn, job.id)

    assert record_chunk_result(conn, job.id, claimed.chunk_key, result={"text": "first"})
    # A retry / duplicate callback must not replace the recorded result.
    assert not record_chunk_result(conn, job.id, claimed.chunk_key, result={"text": "second"})

    loaded = load_job(conn, job.id)
    assert loaded is not None
    chunk = next(c for c in loaded.chunks if c.chunk_key == claimed.chunk_key)
    assert chunk.result == {"text": "first"}


def test_failed_chunk_can_be_recorded_once_then_retried(conn):
    job = _job(conn)
    claimed = claim_next_pending_chunk(conn, job.id)

    assert record_chunk_result(
        conn, job.id, claimed.chunk_key, status="failed", error="provider 500"
    )
    assert not record_chunk_result(
        conn, job.id, claimed.chunk_key, status="failed", error="provider 500"
    )
    loaded = load_job(conn, job.id)
    assert loaded is not None
    # The other chunk is still pending, so the job itself is not terminal yet.
    assert loaded.state == "running"
    chunk = next(c for c in loaded.chunks if c.chunk_key == claimed.chunk_key)
    assert chunk.status == "failed"
    assert chunk.error == "provider 500"

    # A retry may still complete the same chunk; the first result was not final.
    resume_job(conn, job.id)
    assert claim_next_pending_chunk(conn, job.id) is not None
    assert record_chunk_result(conn, job.id, claimed.chunk_key, result={"text": "ok"})
    loaded = load_job(conn, job.id)
    chunk = next(c for c in loaded.chunks if c.chunk_key == claimed.chunk_key)
    assert chunk.status == "complete"
    assert chunk.result == {"text": "ok"}


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_interrupted_run_resumes_only_unfinished_chunks(conn):
    job = _job(conn, chunk_keys=["chunk-1", "chunk-2", "chunk-3"])

    first = claim_next_pending_chunk(conn, job.id)
    record_chunk_result(conn, job.id, first.chunk_key, result={"text": "kept"})
    # Worker claims chunk-2 and dies before recording a result.
    interrupted = claim_next_pending_chunk(conn, job.id)
    assert interrupted is not None and interrupted.chunk_key == "chunk-2"

    resumed = resume_job(conn, job.id)

    assert resumed.state == "running"
    by_key = {chunk.chunk_key: chunk for chunk in resumed.chunks}
    assert by_key["chunk-1"].status == "complete"
    assert by_key["chunk-1"].result == {"text": "kept"}
    assert by_key["chunk-2"].status == "pending"
    assert by_key["chunk-3"].status == "pending"

    # The first claim after resume is the interrupted chunk, never chunk-1.
    next_claim = claim_next_pending_chunk(conn, job.id)
    assert next_claim is not None and next_claim.chunk_key == "chunk-2"
    assert next_claim.attempt == 2


def test_resume_does_not_reopen_completed_or_cancelled_jobs(conn):
    completed = _job(conn, chunk_keys=["only"])
    record_chunk_result(conn, completed.id, "only", result={})
    assert resume_job(conn, completed.id).state == "completed"

    cancelled = _job(conn, chunk_keys=["other"])
    cancel_job(conn, cancelled.id)
    resumed = resume_job(conn, cancelled.id)
    assert resumed.state == "cancelled"
    assert claim_next_pending_chunk(conn, cancelled.id) is None


# ---------------------------------------------------------------------------
# New run preserves the earlier run
# ---------------------------------------------------------------------------


def test_second_run_never_deletes_or_overwrites_the_first_run(conn):
    first = _job(conn, request={"prompt_version": "v1"}, chunk_keys=["chunk-1"])
    record_chunk_result(conn, first.id, "chunk-1", result={"text": "first-run"})

    second = _job(conn, request={"prompt_version": "v2"}, chunk_keys=["chunk-1"])
    assert second.id != first.id
    assert record_chunk_result(conn, second.id, "chunk-1", result={"text": "second-run"})

    first_after = load_job(conn, first.id)
    second_after = load_job(conn, second.id)
    assert first_after is not None and second_after is not None
    assert first_after.state == "completed"
    assert second_after.state == "completed"
    assert first_after.request["prompt_version"] == "v1"
    assert second_after.request["prompt_version"] == "v2"
    assert first_after.chunks[0].result == {"text": "first-run"}
    assert second_after.chunks[0].result == {"text": "second-run"}
    assert len(list_jobs(conn)) == 2


# ---------------------------------------------------------------------------
# Cancellation / timeout / progress
# ---------------------------------------------------------------------------


def test_cancel_is_terminal_and_distinguishable(conn):
    job = _job(conn)
    cancelled = cancel_job(conn, job.id)

    assert cancelled.state == "cancelled"
    assert cancelled.error == "cancelled"
    assert claim_next_pending_chunk(conn, job.id) is None
    # Cancelling twice stays cancelled.
    assert cancel_job(conn, job.id).state == "cancelled"


def test_timeout_and_provider_failure_states_are_distinct(conn):
    timeout_job = _job(conn, chunk_keys=[])
    update_job(conn, timeout_job.id, state="timed_out", error="timeout")
    failed_job = _job(conn, chunk_keys=[])
    update_job(conn, failed_job.id, state="failed", error="provider 500")

    assert load_job(conn, timeout_job.id).state == "timed_out"
    assert load_job(conn, failed_job.id).state == "failed"
    assert set(JOB_STATES) == {
        "pending",
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
    }
    assert set(CHUNK_STATES) == {"pending", "running", "complete", "failed"}


def test_update_job_merges_progress(conn):
    job = _job(conn, chunk_keys=[])
    update_job(conn, job.id, progress={"percent": 40, "message": "Collecting"})
    updated = update_job(conn, job.id, progress={"percent": 60})

    assert updated.progress == {"percent": 60, "message": "Collecting"}


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


def test_migration_24_is_idempotent_and_rerunnable(tmp_path):
    connection = open_connection(tmp_path / "index_v2.sqlite")
    try:
        latest = apply_migrations(connection)
        assert latest >= 24
        assert current_version(connection) == latest

        job = _job(connection)
        record_chunk_result(connection, job.id, "chunk-1", result={"text": "kept"})

        # A second full run records nothing new and keeps every row.
        assert apply_migrations(connection) == latest
        assert apply_migrations(connection) == latest
        assert load_job(connection, job.id).chunks[0].result == {"text": "kept"}
        versions = [
            row[0]
            for row in connection.execute("SELECT version FROM schema_version ORDER BY version")
        ]
        assert versions == list(range(1, latest + 1))
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"analysis_jobs", "analysis_job_chunks"} <= tables
    finally:
        connection.close()
