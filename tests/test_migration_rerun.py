"""A migration run that fails part-way can be re-run to the latest schema version.

Migration DDL runs in autocommit mode before the bookkeeping transaction, so a
failure after the first DDL statement leaves that statement applied while the
version stays put. The runner must recover on a plain re-run because every step
is idempotent.
"""

from __future__ import annotations

import sqlite3

import pytest

from lore.storage import schema as schema_module
from lore.storage.schema import MIGRATIONS, apply_migrations, current_version, open_connection

FULL_MIGRATIONS = list(MIGRATIONS)
# Migration 17 adds several columns. Fail on the second statement, so the first
# DDL (ALTER ... ADD COLUMN origin) has already been applied when the run stops.
TARGET_VERSION = 17
FAIL_ON = "ALTER TABLE sessions ADD COLUMN parent_session_id"


class _FailOnceConn:
    """Proxy that raises once when a statement starting with ``FAIL_ON`` runs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.fired = False

    def execute(self, sql: str, *args):
        if not self.fired and sql.strip().startswith(FAIL_ON):
            self.fired = True
            raise sqlite3.OperationalError("injected failure after first DDL")
        return self._conn.execute(sql, *args)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_failed_migration_run_can_be_rerun_to_latest(tmp_path, monkeypatch):
    conn = open_connection(tmp_path / "v2.db")

    # Bring the DB to just before migration 17 using the real migration list.
    monkeypatch.setattr(schema_module, "MIGRATIONS", FULL_MIGRATIONS[: TARGET_VERSION - 1])
    assert apply_migrations(conn) == TARGET_VERSION - 1

    # Run with the full list and inject a failure after the first DDL of 17.
    monkeypatch.setattr(schema_module, "MIGRATIONS", FULL_MIGRATIONS)
    failing = _FailOnceConn(conn)
    with pytest.raises(sqlite3.OperationalError, match="injected failure"):
        apply_migrations(failing)  # type: ignore[arg-type]
    assert failing.fired

    # Real guarantee: the version is not bumped, and the first DDL stays applied.
    assert current_version(conn) == TARGET_VERSION - 1
    assert "origin" in _columns(conn, "sessions")
    assert "parent_session_id" not in _columns(conn, "sessions")

    # Plain re-run must succeed and reach the latest version.
    latest = len(FULL_MIGRATIONS)
    assert apply_migrations(conn) == latest
    assert current_version(conn) == latest

    sessions_cols = _columns(conn, "sessions")
    assert {"origin", "parent_session_id", "relation", "parser_version"} <= sessions_cols
    assert "reasoning" in _columns(conn, "messages")
    versions = [
        row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")
    ]
    assert versions == list(range(1, latest + 1))  # each version recorded exactly once

    # And a further run on an up-to-date DB is a no-op.
    assert apply_migrations(conn) == latest
    conn.close()
