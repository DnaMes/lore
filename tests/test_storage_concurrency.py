"""Concurrency + migration-robustness tests for the v2 store (#40, #46).

#40 — a writer and a reader (or two writers) overlap in the real app
      (gunicorn 1 worker / 8 threads + the watcher thread). busy_timeout
      must make them wait rather than fail with SQLITE_BUSY.
#46 — a half-applied ALTER TABLE migration must not wedge the runner.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import (
    add_memory,
    apply_migrations,
    current_version,
    initialise,
    open_connection,
    write_sessions,
)
from lore.storage.schema import _run_migration_sql


def _session(sid, n=2):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid} realistic title",
        messages=[
            UnifiedMessage(role=Role.USER, content=f"body {i}", timestamp=datetime(2026, 1, 1))
            for i in range(n)
        ],
    )


# ---------------------------------------------------------------------------
# #40 — busy_timeout
# ---------------------------------------------------------------------------


def test_open_connection_sets_busy_timeout(tmp_path):
    conn = open_connection(tmp_path / "v2.sqlite")
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 5000
    conn.close()


def test_concurrent_writes_do_not_raise_busy(tmp_path):
    """Two threads writing the v2 store must serialise via busy_timeout,
    not fail with SQLITE_BUSY."""
    db = tmp_path / "v2.sqlite"
    initialise(db).close()  # create + migrate up front

    errors: list[Exception] = []

    def _write(prefix):
        try:
            for i in range(5):
                write_sessions(db, [_session(f"{prefix}-{i}")])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent writes raised: {errors}"


def _inject_commit_during_hash(tmp_path, monkeypatch):
    """Make the freshness check commit another write halfway through its reads."""
    from lore.storage import freshness
    from lore.storage.writer import v2_db_path as _db_path

    db = _db_path(tmp_path)
    real = freshness.compute_content_hash
    raced: list[bool] = []

    def racing(conn):
        if not raced:
            raced.append(True)
            write_sessions(db, [_session("committed-in-between")])
        return real(conn)

    monkeypatch.setattr(freshness, "compute_content_hash", racing)
    return raced


def test_store_freshness_reads_stamp_and_rows_in_one_snapshot(tmp_path, monkeypatch):
    """A commit between the two reads must not look like a tampered store (#48)."""
    from lore.storage import freshness
    from lore.storage.writer import v2_db_path

    db = v2_db_path(tmp_path)
    initialise(db).close()
    write_sessions(db, [_session("first")])
    raced = _inject_commit_during_hash(tmp_path, monkeypatch)

    report = freshness.store_freshness(tmp_path)

    assert raced, "the probe never injected a concurrent write"
    assert report["ok"] is True, report["reasons"]
    assert report["hash_matches"] is True


def test_v2_is_available_reads_stamp_and_rows_in_one_snapshot(tmp_path, monkeypatch):
    """The reader's gate has the same race, so it takes the same snapshot."""
    from lore.storage.reader import v2_is_available
    from lore.storage.writer import v2_db_path

    db = v2_db_path(tmp_path)
    initialise(db).close()
    write_sessions(db, [_session("first")])
    raced = _inject_commit_during_hash(tmp_path, monkeypatch)

    available = v2_is_available(tmp_path)

    assert raced, "the probe never injected a concurrent write"
    assert available is True


def test_reader_during_write_sees_consistent_state(tmp_path):
    """A reader running while writes happen must never see a half-written
    store — it sees either the pre- or post-write committed state."""
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("seed")])

    bad: list[str] = []
    stop = threading.Event()

    def _reader():
        while not stop.is_set():
            conn = open_connection(db)
            try:
                # A multi-query reader must take ONE consistent snapshot:
                # without BEGIN, each execute() under WAL sees the latest
                # commit, so a full-replace write between the two queries
                # could show a session row whose messages were just deleted.
                conn.execute("BEGIN")
                rows = conn.execute(
                    "SELECT id, messages_count FROM sessions WHERE messages_synced=1"
                ).fetchall()
                for sid, mcount in rows:
                    actual = conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
                    ).fetchone()[0]
                    if actual != mcount:
                        bad.append(f"{sid}: {actual} != {mcount}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                bad.append(str(exc))
            finally:
                conn.close()

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        for i in range(15):
            write_sessions(db, [_session(f"s-{i}", n=3)])
    finally:
        stop.set()
        reader.join()

    assert bad == [], f"reader saw inconsistent state: {bad[:5]}"


def test_memory_write_during_session_write(tmp_path):
    """add_memory (a writer) overlapping write_sessions must not raise."""
    db_dir = tmp_path
    write_sessions(db_dir / "index_v2.sqlite", [_session("seed")])

    errors: list[Exception] = []

    def _sync():
        try:
            for i in range(8):
                write_sessions(db_dir / "index_v2.sqlite", [_session(f"sync-{i}")])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _memory():
        try:
            for i in range(8):
                add_memory(db_dir, "note", f"Memory {i}", "concurrent memory body")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_sync)
    t2 = threading.Thread(target=_memory)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"overlapping writers raised: {errors}"


# ---------------------------------------------------------------------------
# #46 — migration idempotency
# ---------------------------------------------------------------------------


def test_run_migration_sql_skips_existing_column(tmp_path):
    """ALTER TABLE ADD COLUMN for an existing column is silently skipped."""
    conn = initialise(tmp_path / "v2.sqlite")
    # messages_synced already exists (migration 8) — re-adding must not raise.
    _run_migration_sql(conn, "ALTER TABLE sessions ADD COLUMN messages_synced INTEGER")
    conn.close()


def test_half_applied_alter_migration_recovers(tmp_path):
    """A migration whose ALTER applied but whose schema_version row was
    never written must be safely re-runnable (#46)."""
    db = tmp_path / "v2.sqlite"
    conn = initialise(db)
    full_version = current_version(conn)

    # Simulate a half-applied migration 8: the column exists, the
    # bookkeeping row is gone.
    conn.execute("DELETE FROM schema_version WHERE version = 8")
    conn.close()

    # Re-applying must not raise 'duplicate column name' and must restore
    # the version.
    conn = open_connection(db)
    restored = apply_migrations(conn)
    assert restored == full_version
    conn.close()


def test_run_migration_sql_runs_create_table(tmp_path):
    """Non-ALTER statements still execute normally."""
    conn = initialise(tmp_path / "v2.sqlite")
    _run_migration_sql(conn, "CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='probe'").fetchone() is not None
    conn.close()
