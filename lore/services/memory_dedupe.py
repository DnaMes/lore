"""Find exact confirmed memory duplicates without deleting their audit trail."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..storage.memory import MemoryEntry, _load_entries, set_memory_status
from ..storage.writer import v2_db_path


def _updated_utc(value: str) -> datetime:
    """Parse a stored timestamp; naive values are treated as UTC so mixed rows compare."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def memory_key(title: str, body: str) -> tuple[str, str]:
    """Normalize whitespace and escaped quotes; titles also ignore case."""

    def normalize(text: str) -> str:
        return " ".join(text.replace(r"\"", '"').replace(r"\'", "'").split())

    return normalize(title).casefold(), hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()


def confirmed_memories(output_dir: Path) -> list[MemoryEntry]:
    """Read all active confirmed entries without creating or migrating the store."""
    db = v2_db_path(output_dir)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM memory WHERE status = 'confirmed' AND superseded_by IS NULL ORDER BY id"
        ).fetchall()
        return _load_entries(conn, rows)
    finally:
        conn.close()


@dataclass
class DuplicateGroup:
    """One normalized title/body group and its chosen surviving memory."""

    title: str
    kept_id: int
    rejected_ids: list[int]


def dedupe_memories(output_dir: Path, *, apply: bool = False) -> list[DuplicateGroup]:
    """Plan duplicate rejections; apply them only when explicitly requested."""
    by_key: dict[tuple[str, str], list[MemoryEntry]] = {}
    for entry in confirmed_memories(output_dir):
        by_key.setdefault(memory_key(entry.title, entry.body), []).append(entry)

    groups: list[DuplicateGroup] = []
    for (title, _), entries in by_key.items():
        if len(entries) < 2:
            continue
        kept = max(
            entries,
            key=lambda entry: (
                entry.feedback_weight,
                len(entry.scope_project or ""),
                _updated_utc(entry.updated),
                entry.id,
            ),
        )
        rejected_ids = [entry.id for entry in entries if entry.id != kept.id]
        if apply:
            for memory_id in rejected_ids:
                if not set_memory_status(
                    output_dir, memory_id, "rejected", from_status="confirmed"
                ):
                    raise ValueError(f"memory {memory_id} no longer exists")
        groups.append(DuplicateGroup(title, kept.id, rejected_ids))
    return groups
