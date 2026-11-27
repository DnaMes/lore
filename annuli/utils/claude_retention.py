"""Read-only Claude Code transcript retention settings for ``lore check`` (#197)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

SETTINGS_MAX_BYTES = 256 * 1024
DEFAULT_CLEANUP_DAYS = 30
NEAR_DELETION_DAYS = 5
_TRANSCRIPT_SCAN_LIMIT = 20_000

SNIPPET_CLEANUP_365 = '{"cleanupPeriodDays": 365}'


def claude_config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude"


def claude_projects_dir() -> Path:
    return claude_config_dir() / "projects"


def _read_settings_file(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > SETTINGS_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_cleanup_period_days() -> Tuple[Optional[int], str]:
    """Return ``(days, kind)`` where ``kind`` is ``ok``, ``unset``, or ``invalid``."""
    base = claude_config_dir()
    for name in ("settings.json", "settings.local.json"):
        data = _read_settings_file(base / name)
        if not data or "cleanupPeriodDays" not in data:
            continue
        raw = data["cleanupPeriodDays"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, "invalid"
        if raw <= 0:
            return raw, "invalid"
        return raw, "ok"
    return None, "unset"


def _iter_transcript_files(projects: Path) -> Iterator[Path]:
    if not projects.is_dir():
        return
    count = 0
    for project_dir in projects.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            yield path
            count += 1
            if count >= _TRANSCRIPT_SCAN_LIMIT:
                return
        for path in project_dir.glob("*/subagents/*.jsonl"):
            yield path
            count += 1
            if count >= _TRANSCRIPT_SCAN_LIMIT:
                return


def count_transcripts_within_days_of_deletion(retention_days: int) -> int:
    """Count transcript files older than ``retention_days - NEAR_DELETION_DAYS``."""
    threshold_days = max(0, retention_days - NEAR_DELETION_DAYS)
    cutoff = time.time() - threshold_days * 86400
    projects = claude_projects_dir()
    total = 0
    for path in _iter_transcript_files(projects):
        try:
            if path.stat().st_mtime < cutoff:
                total += 1
        except OSError:
            continue
    return total


def has_claude_transcripts() -> bool:
    projects = claude_projects_dir()
    for _ in _iter_transcript_files(projects):
        return True
    return False


def retention_check_lines() -> List[str]:
    """Retention lines for ``lore check``; empty when Claude projects have no transcripts."""
    try:
        return _retention_lines()
    except OSError:
        return []


def _retention_lines() -> List[str]:
    if not has_claude_transcripts():
        return []

    days, kind = read_cleanup_period_days()
    if kind == "ok" and days is not None and days > 30:
        return [f"Retention: cleanupPeriodDays={days} (OK)"]

    if kind == "invalid":
        effective = DEFAULT_CLEANUP_DAYS
        lines = [
            "Retention: cleanupPeriodDays has an invalid value; "
            f"Claude Code deletes session transcripts after {effective} days (default 30). "
            'Raise "cleanupPeriodDays" in ~/.claude/settings.json to keep them; '
            "Lore only archives what it has already synced.",
            f"  Add to ~/.claude/settings.json: {SNIPPET_CLEANUP_365}",
        ]
        near = count_transcripts_within_days_of_deletion(effective)
        if near:
            lines.append(f"  {near} transcripts are within 5 days of deletion or past the limit")
        return lines

    effective = days if (kind == "ok" and days is not None) else DEFAULT_CLEANUP_DAYS
    if kind == "unset" or (days is not None and days <= 30):
        lines = [
            f"Retention: Claude Code deletes session transcripts after {effective} days "
            f"(default {DEFAULT_CLEANUP_DAYS}). "
            'Raise "cleanupPeriodDays" in ~/.claude/settings.json to keep them; '
            "Lore only archives what it has already synced.",
            f"  Add to ~/.claude/settings.json: {SNIPPET_CLEANUP_365}",
        ]
        near = count_transcripts_within_days_of_deletion(effective)
        if near:
            lines.append(f"  {near} transcripts are within 5 days of deletion or past the limit")
        return lines

    return []
