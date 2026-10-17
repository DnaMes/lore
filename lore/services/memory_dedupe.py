"""Find exact confirmed memory duplicates without deleting their audit trail."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..storage.memory import (
    MemoryEntry,
    _load_entries,
    reject_memories_bulk,
    restore_rejected_memories_bulk,
)
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


def _plan_duplicate_groups(output_dir: Path) -> list[DuplicateGroup]:
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
        groups.append(DuplicateGroup(title, kept.id, rejected_ids))
    return groups


def _write_undo_log(output_dir: Path, groups: list[DuplicateGroup]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"dedupe-undo-{stamp}.json"
    payload = {
        "groups": [
            {"title": group.title, "kept_id": group.kept_id, "rejected_ids": group.rejected_ids}
            for group in groups
        ]
    }
    # Exclusive create: two applies must never overwrite each other's undo log.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
    return path


def dedupe_memories(
    output_dir: Path, *, apply: bool = False
) -> tuple[list[DuplicateGroup], Path | None]:
    """Plan duplicate rejections; apply them atomically only when explicitly requested."""
    groups = _plan_duplicate_groups(output_dir)
    undo_path: Path | None = None
    if apply and groups:
        reject_ids = [memory_id for group in groups for memory_id in group.rejected_ids]
        if reject_ids:
            undo_path = _write_undo_log(output_dir, groups)
            try:
                reject_memories_bulk(output_dir, reject_ids)
            except Exception:
                undo_path.unlink(missing_ok=True)  # nothing was rejected: no stale log
                raise
    return groups, undo_path


def restore_dedupe_undo(output_dir: Path, undo_file: Path) -> tuple[int, int, int]:
    """Restore rejections recorded in a dedupe undo log (idempotent)."""
    data = json.loads(undo_file.expanduser().read_text(encoding="utf-8"))
    reject_ids: list[int] = []
    try:
        for group in data["groups"]:
            reject_ids.extend(int(memory_id) for memory_id in group["rejected_ids"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"malformed dedupe undo log {undo_file}: {exc!r}") from exc
    return restore_rejected_memories_bulk(output_dir, reject_ids)
