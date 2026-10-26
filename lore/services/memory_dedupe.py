"""Find exact confirmed memory duplicates without deleting their audit trail.

Identity rule: two confirmed memories are duplicates only when *every* content
and scoping dimension matches — the normalized (whitespace/escaped-quote
collapsed, casefolded) title, the SHA-256 of the normalized body, the ``kind``,
and the normalized ``scope_project`` and ``scope_tool`` (``None`` means the
global/empty scope). Memories are never merged across different scopes: a
memory scoped to ``/work/a`` is invisible to a recall for ``/work/b`` (or for
the global scope), so a global memory and an identical project-scoped copy are
separate memories and both must survive dedupe. ``author`` is provenance, not
scoping, so it is deliberately not part of the identity key.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
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


def _normalize(text: str) -> str:
    """Collapse whitespace and unescape the quotes used in markdown bodies."""
    return " ".join(text.replace(r"\"", '"').replace(r"\'", "'").split())


def memory_key(title: str, body: str) -> tuple[str, str]:
    """Normalize whitespace and escaped quotes; titles also ignore case."""
    return _normalize(title).casefold(), hashlib.sha256(
        _normalize(body).encode("utf-8")
    ).hexdigest()


def dedupe_identity(entry: MemoryEntry) -> tuple[str, str, str, str, str]:
    """Full dedupe identity: content key plus kind and scope; never merged across scopes."""
    title, body_hash = memory_key(entry.title, entry.body)
    return (
        title,
        body_hash,
        _normalize(entry.kind).casefold(),
        # Paths are case-sensitive on Linux, so scopes are not casefolded.
        _normalize(entry.scope_project or ""),
        _normalize(entry.scope_tool or ""),
    )


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
    by_key: dict[tuple[str, str, str, str, str], list[MemoryEntry]] = {}
    for entry in confirmed_memories(output_dir):
        by_key.setdefault(dedupe_identity(entry), []).append(entry)

    groups: list[DuplicateGroup] = []
    for key, entries in by_key.items():
        if len(entries) < 2:
            continue
        # Every entry shares kind and scope, so only feedback, recency and id break ties.
        kept = max(
            entries,
            key=lambda entry: (entry.feedback_weight, _updated_utc(entry.updated), entry.id),
        )
        rejected_ids = [entry.id for entry in entries if entry.id != kept.id]
        groups.append(DuplicateGroup(key[0], kept.id, rejected_ids))
    return groups


def _store_identity(output_dir: Path) -> dict[str, str]:
    """Stable store identity for undo logs.

    The schema has no store uuid, so the resolved SQLite path doubles as the
    fingerprint; both fields carry it to keep the log format explicit.
    """
    resolved = str(v2_db_path(output_dir).resolve())
    return {"db_path": resolved, "fingerprint": resolved}


def _write_undo_log(output_dir: Path, groups: list[DuplicateGroup]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"dedupe-undo-{stamp}.json"
    payload = {
        "store": _store_identity(output_dir),
        "groups": [
            {"title": group.title, "kept_id": group.kept_id, "rejected_ids": group.rejected_ids}
            for group in groups
        ],
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
    logged_store = data.get("store") if isinstance(data, dict) else None
    if not logged_store:
        # Written before store fingerprints existed; accepted with a warning.
        print(
            f"Warning: dedupe undo log {undo_file} has no store identity "
            "(pre-fingerprint log); restoring anyway.",
            file=sys.stderr,
        )
    else:
        current = _store_identity(output_dir)["fingerprint"]
        logged = logged_store.get("fingerprint") if isinstance(logged_store, dict) else None
        if logged != current:
            raise ValueError(
                f"dedupe undo log {undo_file} belongs to store {logged!r}, not {current!r}; "
                "refusing to restore into a different store"
            )
    reject_ids: list[int] = []
    try:
        for group in data["groups"]:
            reject_ids.extend(int(memory_id) for memory_id in group["rejected_ids"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"malformed dedupe undo log {undo_file}: {exc!r}") from exc
    return restore_rejected_memories_bulk(output_dir, reject_ids)
