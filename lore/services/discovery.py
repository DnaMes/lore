"""Per-tool discovery telemetry: record it at sync time, read it back for ``lore check``.

The counters come from ``BaseExtractor.discovery``. They are stored in the v2
``store_meta`` table under ``discovery:<tool>`` so ``lore check`` can show the last
sync run without scanning. Recording is best-effort and never creates the database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from lore.extractors.base import BaseExtractor
from lore.storage.writer import v2_db_path

logger = logging.getLogger(__name__)

KEY_PREFIX = "discovery:"


def discovery_snapshot(extractor: BaseExtractor) -> dict[str, Any]:
    """The counters of the extractor's last run as a JSON-serialisable dict."""
    return {
        **getattr(extractor, "discovery", {}),
        "sessions": getattr(extractor, "sessions_yielded", 0),
        "skip_counts": dict(getattr(extractor, "skip_counts", {}) or {}),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def record_discovery(output_dir: Path, extractors: Iterable[BaseExtractor]) -> None:
    """Write each extractor's counters to ``store_meta``; never raises, never creates the DB."""
    try:
        db = v2_db_path(output_dir)
        if not db.exists():
            return
        rows = [
            (f"{KEY_PREFIX}{extractor.tool.value}", json.dumps(discovery_snapshot(extractor)))
            for extractor in extractors
        ]
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            conn.executemany(
                "INSERT INTO store_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Could not record discovery telemetry: %s", type(exc).__name__)


def read_last_discovery(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Last recorded run per tool value; ``{}`` when there is no store or no record."""
    db = v2_db_path(output_dir)
    if not db.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT key, value FROM store_meta WHERE key LIKE ?", (f"{KEY_PREFIX}%",)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    for key, value in rows:
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            result[str(key)[len(KEY_PREFIX) :]] = data
    return result
