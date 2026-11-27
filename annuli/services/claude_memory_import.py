"""Seed the shared memory store from Claude Code auto-memory files (``memory import-claude``).

Each ``<root>/<slug>/memory/*.md`` file has a YAML-style frontmatter block (``name``,
``description``, ``metadata.type``) followed by a body. ``MEMORY.md`` is an index and is
skipped. Provenance is stored in the memory metadata as ``{"claude_import": {"path",
"sha256", "type"}}``; an unchanged file is never written twice, and a changed file is
written as a new memory that supersedes the previous import.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..extractors.claude import ClaudeCodeExtractor
from ..storage.memory import add_memory
from ..storage.writer import v2_db_path
from ..utils.redact import redact
from .memory_dedupe import confirmed_memories, memory_key

# Claude auto-memory type -> closest accepted memory kind.
KIND_BY_TYPE = {
    "user": "fact",
    "feedback": "lesson",
    "project": "fact",
    "reference": "link",
}
INDEX_FILE_NAME = "MEMORY.md"
IMPORT_KEY = "claude_import"
SKIP_REASONS = ("MEMORY.md index", "no frontmatter", "empty body", "unreadable")


@dataclass
class ImportItem:
    """One memory file that can be written (new or changed)."""

    path: Path
    sha256: str
    mtype: str
    kind: str
    title: str
    body: str
    scope_project: Optional[str]
    status: str
    redactions: int


@dataclass
class ImportReport:
    """Counts for one run; nothing here is file content."""

    root: Path
    scanned: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    new: int = 0
    updated: int = 0
    already: int = 0
    duplicates: int = 0
    failed: int = 0
    redactions: int = 0
    notes: List[str] = field(default_factory=list)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_frontmatter(text: str) -> Optional[Tuple[Dict[str, str], str]]:
    """Return ``(fields, body)`` or ``None`` when there is no closed frontmatter block.

    Top-level keys are stored as ``key``; keys nested one level down (``metadata:``) as
    ``parent.key``. Lines that are not ``key: value`` are ignored.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    fields: Dict[str, str] = {}
    parent: Optional[str] = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.strip().partition(":")
        key = key.strip()
        if not sep or not key:
            continue
        value = _unquote(value.strip())
        if line[:1] in (" ", "\t"):
            if parent is not None:
                fields[f"{parent}.{key}"] = value
        else:
            fields[key] = value
            parent = key if value == "" else None
    return fields, "\n".join(lines[end + 1 :]).strip()


def _scope_for_slug(slug: str) -> Optional[str]:
    """Map a Claude project slug to its project path, only when that path exists on disk."""
    decoded = ClaudeCodeExtractor()._decode_project_name(slug)
    return decoded if os.path.isdir(decoded) else None


def _existing_imports(output_dir: Path) -> Dict[str, Tuple[int, str, str]]:
    """Latest imported row per source path: ``path -> (id, sha256, status)``.

    Read-only, so a dry run never creates or migrates the database.
    """
    db = v2_db_path(output_dir)
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT id, status, metadata_json FROM memory WHERE metadata_json LIKE ? ORDER BY id",
            (f'%"{IMPORT_KEY}"%',),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    latest: Dict[str, Tuple[int, str, str]] = {}
    for memory_id, status, metadata_json in rows:
        try:
            info = json.loads(metadata_json)[IMPORT_KEY]
            path_key = str(Path(str(info["path"])).resolve())
            latest[path_key] = (int(memory_id), str(info["sha256"]), str(status))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return latest


def _source_path_key(path: Path) -> str:
    """Canonical import source path (relative and absolute forms match)."""
    return str(path.resolve())


def _plan_file(
    path: Path, scope_project: Optional[str]
) -> Tuple[Optional[str], Optional[ImportItem]]:
    """Return ``(skip_reason, None)`` or ``(None, item)`` for one memory file."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable", None
    parsed = _split_frontmatter(text)
    if parsed is None:
        return "no frontmatter", None
    fields, body_text = parsed
    description = fields.get("description", "")
    if not description and not body_text:
        return "empty body", None

    mtype = fields.get("metadata.type") or fields.get("type") or ""
    name = fields.get("name", "")
    title = name or description or path.stem
    combined = "\n\n".join(part for part in (description, body_text) if part)
    # Candidate when the type is unrecognised or the frontmatter gave no usable title.
    recognised = mtype in KIND_BY_TYPE
    status = "confirmed" if recognised and (name or description) else "candidate"

    clean_title = redact(title)
    clean_body = redact(combined)
    return None, ImportItem(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        mtype=mtype if recognised else "unknown",
        kind=KIND_BY_TYPE.get(mtype, "note"),
        title=clean_title.text,
        body=clean_body.text,
        scope_project=scope_project,
        status=status,
        redactions=clean_title.count + clean_body.count,
    )


def import_claude_memory(output_dir: Path, root: Path, *, apply: bool = False) -> ImportReport:
    """Scan ``root/<slug>/memory/*.md`` and import new or changed files.

    Without ``apply`` nothing is written. Returns counts only; file bodies are never
    returned or printed.
    """
    report = ImportReport(root=root)
    existing = _existing_imports(output_dir)
    active = {entry.id: entry for entry in confirmed_memories(output_dir)}
    known_content = Counter(memory_key(entry.title, entry.body) for entry in active.values())
    scope_cache: Dict[str, Optional[str]] = {}

    for path in sorted(root.glob("*/memory/*.md")):
        if not path.is_file():
            continue
        report.scanned += 1
        if path.name == INDEX_FILE_NAME:
            report.skipped["MEMORY.md index"] = report.skipped.get("MEMORY.md index", 0) + 1
            continue
        slug = path.parent.parent.name
        if slug not in scope_cache:
            scope_cache[slug] = _scope_for_slug(slug)
        reason, item = _plan_file(path, scope_cache[slug])
        if item is None:
            key = reason or "unreadable"
            report.skipped[key] = report.skipped.get(key, 0) + 1
            continue
        report.by_type[item.mtype] = report.by_type.get(item.mtype, 0) + 1

        prev = existing.get(_source_path_key(path))
        if prev is not None and prev[1] == item.sha256:
            report.already += 1
            continue
        key = memory_key(item.title, item.body)
        # A changed, already-imported file must still supersede its old import (and refresh
        # the sha), even when its content now matches another memory.
        if prev is None and known_content[key] > 0:
            report.duplicates += 1
            continue
        if apply:
            if not _write(output_dir, item, prev, report):
                continue
        elif prev is None:
            report.new += 1
            report.redactions += item.redactions
        else:
            report.updated += 1
            report.redactions += item.redactions
        if item.status == "confirmed":
            previous = active.get(prev[0]) if prev else None
            if previous is not None and previous.scope_project == item.scope_project:
                known_content[memory_key(previous.title, previous.body)] -= 1
            known_content[key] += 1
    return report


def _write(
    output_dir: Path,
    item: ImportItem,
    prev: Optional[Tuple[int, str, str]],
    report: ImportReport,
) -> bool:
    """Write one item; on a changed file, supersede the previous import when allowed."""
    kwargs = dict(
        kind=item.kind,
        title=item.title,
        body=item.body,
        author="claude-import",
        scope_project=item.scope_project,
        scope_tool="claude",
        tags=[item.mtype, "claude-memory"],
        metadata={
            IMPORT_KEY: {
                "path": _source_path_key(item.path),
                "sha256": item.sha256,
                "type": item.mtype,
            }
        },
        status=item.status,
    )
    try:
        if prev is None:
            add_memory(output_dir, **kwargs)
        else:
            try:
                add_memory(output_dir, supersedes=prev[0], **kwargs)
            except ValueError as exc:
                report.notes.append(
                    f"note: could not supersede #{prev[0]} for {item.path.name} ({exc}); "
                    "added as a new memory"
                )
                add_memory(output_dir, **kwargs)
    except ValueError as exc:
        report.failed += 1
        report.notes.append(f"note: skipped {item.path.name}: {exc}")
        return False
    if prev is None:
        report.new += 1
    else:
        report.updated += 1
    report.redactions += item.redactions
    return True
