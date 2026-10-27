"""How fresh the v2 store is: ``generated_at``, a content hash, and the assertion (#36, #48).

``store_meta.generated_at`` (migration 9) records when the store was last
written; it is what :func:`lore.storage.reader.v2_is_available` compares against
``index.json``'s mtime. A timestamp alone cannot tell whether the *rows* a reader
sees are the ones the writer committed — a row written outside the index build
(a bundle import, a manual fix) leaves the stamp intact. So every write path also
stamps ``store_meta.content_hash``: a sha256 over the session fields the served
payload is built from, in a stable order.

Read paths stay tolerant (one damaged store must not take the web UI down while
the JSON fallback exists) but never silently: the gate logs why it rejected the
store. :func:`assert_store_fresh` is the loud half — it raises
:class:`StaleStoreError` with every reason, and the index writer calls it right
after committing, so a store that did not persist what was just written fails
the build instead of being served.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import current_version, open_connection

#: ``store_meta`` key holding the content hash (migration 9 table, no new column).
CONTENT_HASH_KEY = "content_hash"
GENERATED_AT_KEY = "generated_at"

#: Migration that introduced ``store_meta``. Below it a store cannot be checked.
STORE_META_VERSION = 9

#: A store stamped at second precision can be up to this much older than the
#: file it is compared with and still be the write that produced it.
STALENESS_GRACE_SECONDS = 2.0


class StaleStoreError(RuntimeError):
    """The v2 store is older than its reference, or its rows changed under its stamp."""


def resolve_store(target: Path) -> Path:
    """The v2 store file for ``target`` — either the file itself or its directory."""
    from .writer import v2_db_path

    return target if target.suffix == ".sqlite" else v2_db_path(target)


def store_cache_key(output_dir: Path) -> tuple:
    """Cache key for a v2 read: main DB file *and* its WAL sidecar.

    In WAL mode a committed write can land in ``-wal`` without changing the main
    file's size or mtime, so keying on the main file alone would serve stale
    data after the first write of a session. Mirrored for the main DB's own
    read caches.
    """
    db = resolve_store(output_dir)
    if not db.exists():
        return (0, 0, 0, 0)
    try:
        stat = db.stat()
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            wal_stat = wal.stat()
            return (stat.st_mtime_ns, stat.st_size, wal_stat.st_mtime_ns, wal_stat.st_size)
        return (stat.st_mtime_ns, stat.st_size, 0, 0)
    except OSError:
        return (0, 0, 0, 0)


def compute_content_hash(conn: sqlite3.Connection) -> str:
    """Deterministic sha256 over the session fields the served payload is built from.

    Order is fixed by ``id``; each row contributes id, updated, the stored
    content hash and the message count. Annotation tables (tags, memory,
    tombstones, pruned ids) are deliberately out: they are read live and a write
    to them does not rebuild the session payload.
    """
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT id, COALESCE(updated, ''), COALESCE(content_sha256, ''), messages_count "
        "FROM sessions ORDER BY id"
    ):
        digest.update("\x1f".join(str(value) for value in tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def stamp_store(conn: sqlite3.Connection) -> str:
    """Write ``generated_at`` and ``content_hash`` into ``store_meta``.

    Call inside the write transaction, after the last session row: both stamps
    must describe the transaction that is about to commit. Returns the hash.
    """
    digest = compute_content_hash(conn)
    if current_version(conn) < STORE_META_VERSION:
        return digest
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (GENERATED_AT_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CONTENT_HASH_KEY, digest),
    )
    return digest


def _read_meta(conn: sqlite3.Connection) -> Dict[str, Optional[str]]:
    try:
        rows = conn.execute("SELECT key, value FROM store_meta").fetchall()
    except sqlite3.Error:
        return {GENERATED_AT_KEY: None, CONTENT_HASH_KEY: None}
    meta = {str(row[0]): row[1] for row in rows}
    return {
        GENERATED_AT_KEY: meta.get(GENERATED_AT_KEY),
        CONTENT_HASH_KEY: meta.get(CONTENT_HASH_KEY),
    }


def store_freshness(target: Path, compare_to: Optional[Path] = None) -> Dict[str, Any]:
    """Report whether the v2 store can be trusted, with the reasons it cannot.

    ``target`` is the store file (``index_v2.sqlite``) or the directory holding
    it. ``compare_to`` is typically ``index.json``: a store stamped before that
    file was last written is stale (it predates the archive the JSON describes).
    The return value always carries the same keys, so callers can assert on it::

        {"exists", "generated_at", "stored_hash", "computed_hash",
         "hash_matches", "stale_vs_json", "reasons", "notes", "ok"}
    """
    db = resolve_store(target)
    report: Dict[str, Any] = {
        "exists": db.exists(),
        "generated_at": None,
        "stored_hash": None,
        "computed_hash": None,
        "hash_matches": None,
        "stale_vs_json": False,
        "reasons": [],
        "notes": [],
        "ok": False,
    }
    reasons: List[str] = report["reasons"]
    notes: List[str] = report["notes"]
    if not db.exists():
        reasons.append(f"no v2 store at {db}")
        return report

    conn = open_connection(db)
    try:
        if current_version(conn) < STORE_META_VERSION:
            reasons.append(f"store schema is older than migration {STORE_META_VERSION}")
            return report
        meta = _read_meta(conn)
        report["generated_at"] = meta[GENERATED_AT_KEY]
        report["stored_hash"] = meta[CONTENT_HASH_KEY]
        report["computed_hash"] = compute_content_hash(conn)
    finally:
        conn.close()

    generated_at = report["generated_at"]
    if not generated_at:
        reasons.append("store has no generated_at stamp")
    elif compare_to is not None and compare_to.exists():
        try:
            stamped = datetime.fromisoformat(str(generated_at)).timestamp()
        except ValueError:
            reasons.append(f"generated_at is not an ISO timestamp: {generated_at!r}")
            stamped = None
        if stamped is not None and stamped < compare_to.stat().st_mtime - STALENESS_GRACE_SECONDS:
            report["stale_vs_json"] = True
            reasons.append(
                f"store was generated at {generated_at}, before {compare_to} was written"
            )

    stored_hash = report["stored_hash"]
    if not stored_hash:
        notes.append("store predates the content hash; rows cannot be verified")
    else:
        report["hash_matches"] = stored_hash == report["computed_hash"]
        if not report["hash_matches"]:
            reasons.append(
                "content hash mismatch: the store says "
                f"{stored_hash[:12]}… but its rows hash to {report['computed_hash'][:12]}…"
            )

    report["ok"] = not reasons
    return report


def assert_store_fresh(target: Path, compare_to: Optional[Path] = None) -> Dict[str, Any]:
    """Raise :class:`StaleStoreError` unless the v2 store is trustworthy.

    Returns the freshness report (see :func:`store_freshness`) when it is, so a
    caller can log the stamps it accepted.
    """
    report = store_freshness(target, compare_to)
    if not report["ok"]:
        raise StaleStoreError("v2 store is not trustworthy: " + "; ".join(report["reasons"]))
    return report
