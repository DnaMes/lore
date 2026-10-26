"""Per-source archive health (#162).

"Synced 400 sessions" hides the interesting part: which source files were
captured, parsed, filtered as low quality, unsupported, failed, unavailable or
explicitly deleted. This module stores one row per source file and aggregates
the counts a user can actually act on.

States are a closed set. A source that changes state keeps its counters, so the
history ("this file failed twice, then parsed") is not lost.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

HEALTH_STATES = (
    "captured",
    "parsed",
    "filtered",
    "unsupported",
    "failed",
    "unavailable",
    "deleted",
)

#: Which counter a state increments.
_STATE_COUNTERS = {
    "captured": "sessions_captured",
    "parsed": "sessions_captured",
    "filtered": "sessions_filtered",
    "unsupported": "sessions_unsupported",
    "failed": "sessions_failed",
    "unavailable": "sessions_failed",
    "deleted": "sessions_filtered",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_source_state(
    conn,
    *,
    source_identity: str,
    tool: str,
    source_path: str,
    state: str,
    detail: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    count: int = 1,
) -> None:
    """Upsert one source's state and increment its counter."""
    if state not in HEALTH_STATES:
        raise ValueError(f"unknown source health state: {state!r}")
    counter = _STATE_COUNTERS[state]
    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None
    conn.execute(
        f"""
        INSERT INTO source_health (
            source_identity, tool, source_path, state, {counter},
            last_captured_at, last_error, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_identity) DO UPDATE SET
            tool = excluded.tool,
            source_path = excluded.source_path,
            state = excluded.state,
            {counter} = source_health.{counter} + excluded.{counter},
            last_captured_at = excluded.last_captured_at,
            last_error = COALESCE(excluded.last_error, source_health.last_error),
            detail_json = COALESCE(excluded.detail_json, source_health.detail_json)
        """,
        (
            source_identity,
            tool,
            source_path,
            state,
            max(1, int(count)),
            _now(),
            error,
            detail_json,
        ),
    )


def state_for_capture(status: str, decode_status: str = "parsed") -> str:
    """Map a capture outcome onto a health state."""
    if status in ("excluded", "unavailable"):
        return "unavailable"
    if decode_status in ("unknown", "malformed", "unparsed", "partial"):
        return "unsupported"
    if decode_status == "stale":
        return "captured"
    return "parsed"


def record_capture_outcome(
    conn,
    *,
    tool: str,
    source_identity: str,
    source_path: str,
    status: str,
    decode_status: str,
    decode_note: Optional[str] = None,
) -> None:
    """Record the health state implied by a capture result."""
    state = state_for_capture(status, decode_status)
    detail = {"capture_status": status, "decode_status": decode_status}
    if decode_note:
        detail["note"] = decode_note
    record_source_state(
        conn,
        source_identity=source_identity,
        tool=tool,
        source_path=source_path,
        state=state,
        detail=detail,
        error=decode_note if state in ("unsupported", "failed") else None,
    )


def record_filtered(
    conn,
    *,
    tool: str,
    source_path: str,
    reason: str,
    source_identity: Optional[str] = None,
) -> None:
    """Record that a decoded source was filtered out of the curated views."""
    record_source_state(
        conn,
        source_identity=source_identity or f"{tool}:local:{source_path}",
        tool=tool,
        source_path=source_path,
        state="filtered",
        detail={"reason": reason},
    )


def record_failure(
    conn, *, tool: str, source_path: str, error: str, source_identity: Optional[str] = None
) -> None:
    record_source_state(
        conn,
        source_identity=source_identity or f"{tool}:local:{source_path}",
        tool=tool,
        source_path=source_path,
        state="failed",
        error=error,
    )


def record_deleted(conn, *, tool: str, source_path: str) -> None:
    record_source_state(
        conn,
        source_identity=f"{tool}:local:{source_path}",
        tool=tool,
        source_path=source_path,
        state="deleted",
    )


def source_health_summary(conn) -> Dict[str, Any]:
    """Counts per state plus totals, for one command's output."""
    rows = conn.execute(
        "SELECT state, COUNT(*) AS sources, "
        "SUM(sessions_captured) AS captured, SUM(sessions_filtered) AS filtered, "
        "SUM(sessions_unsupported) AS unsupported, SUM(sessions_failed) AS failed "
        "FROM source_health GROUP BY state"
    ).fetchall()
    per_state = {
        row["state"]: {
            "sources": int(row["sources"]),
            "sessions_captured": int(row["captured"] or 0),
            "sessions_filtered": int(row["filtered"] or 0),
            "sessions_unsupported": int(row["unsupported"] or 0),
            "sessions_failed": int(row["failed"] or 0),
        }
        for row in rows
    }
    for state in HEALTH_STATES:
        per_state.setdefault(
            state,
            {
                "sources": 0,
                "sessions_captured": 0,
                "sessions_filtered": 0,
                "sessions_unsupported": 0,
                "sessions_failed": 0,
            },
        )
    total = conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0]
    return {"states": per_state, "sources_tracked": int(total)}


def list_source_health(conn, state: Optional[str] = None, limit: int = 200) -> list[Dict[str, Any]]:
    """Per-source records, optionally filtered by state."""
    if state is not None and state not in HEALTH_STATES:
        raise ValueError(f"unknown source health state: {state!r}")
    sql = (
        "SELECT source_identity, tool, source_path, state, sessions_captured, "
        "sessions_filtered, sessions_unsupported, sessions_failed, last_captured_at, "
        "last_error, detail_json FROM source_health"
    )
    params: Iterable[Any] = ()
    if state:
        sql += " WHERE state = ?"
        params = (state,)
    sql += " ORDER BY last_captured_at DESC, source_path LIMIT ?"
    rows = conn.execute(sql, (*params, int(limit))).fetchall()
    return [
        {
            "source_identity": row["source_identity"],
            "tool": row["tool"],
            "source_path": row["source_path"],
            "state": row["state"],
            "sessions_captured": int(row["sessions_captured"]),
            "sessions_filtered": int(row["sessions_filtered"]),
            "sessions_unsupported": int(row["sessions_unsupported"]),
            "sessions_failed": int(row["sessions_failed"]),
            "last_captured_at": row["last_captured_at"],
            "last_error": row["last_error"],
            "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
        }
        for row in rows
    ]


def record_extractor_skips(conn, tool: str, skip_counts: Dict[str, int]) -> None:
    """Record aggregate per-tool skip counts as filtered source rows.

    Extractors count skipped sessions per reason but not per file. Until an
    extractor names the file, the report keeps the count on a per-tool row whose
    path names it as an aggregate, so nothing is silently dropped from the totals.
    """
    for reason, count in (skip_counts or {}).items():
        if not count:
            continue
        record_source_state(
            conn,
            source_identity=f"{tool}:aggregate:filtered",
            tool=tool,
            source_path=f"(aggregate: {reason})",
            state="filtered",
            detail={"reason": reason, "aggregate": True},
            count=int(count),
        )
