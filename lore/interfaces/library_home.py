"""Library-home payloads for the dashboard (B5)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

from lore.interfaces.web_formatting import _parse_local_naive, session_day_iso
from lore.services.index import is_automated
from lore.storage.summaries import list_summaries

CONTINUE_LIMIT = 6
PROJECTS_LIMIT = 12
RECENT_DAYS_LIMIT = 7
DAY_TITLE_LIMIT = 3


def short_project_label(project_path: Optional[str]) -> str:
    """Short label: last two path segments when available."""
    if not project_path:
        return "Unassigned"
    parts = [part for part in Path(project_path).parts if part not in ("", "/")]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    if parts:
        return parts[-1]
    return project_path


def project_page_url(project_path: str) -> str:
    """Deep link into the projects overview for one workspace path."""
    cleaned = (project_path or "").strip()
    if not cleaned or cleaned == "Unassigned":
        return "/projects"
    return "/projects?" + urlencode({"project": cleaned})


def format_relative_activity_time(
    iso_value: Optional[str],
    now: Optional[datetime] = None,
) -> str:
    """Return a compact relative time ('2 h ago') or a calendar date."""
    dt = _parse_local_naive(iso_value)
    if dt is None:
        return ""
    ref = now or datetime.now()
    if ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    delta = ref - dt
    if delta.total_seconds() < 0:
        return dt.date().isoformat()
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        label = "1 min ago" if minutes == 1 else f"{minutes} min ago"
        return label
    hours = minutes // 60
    if hours < 24:
        return "1 h ago" if hours == 1 else f"{hours} h ago"
    if dt.date() == ref.date() - timedelta(days=1):
        return "Yesterday"
    if (ref.date() - dt.date()).days < 7:
        return dt.strftime("%b %d").replace(" 0", " ")
    return dt.date().isoformat()


def _sort_stamp(session: Mapping[str, Any]) -> str:
    return str(session.get("updated") or session.get("created") or "")


def _display_title(session: Mapping[str, Any]) -> str:
    title = session.get("display_title") or session.get("title") or "Untitled Session"
    return str(title)


def _visible_sessions(
    sessions: list[dict[str, Any]],
    *,
    include_automated: bool,
) -> list[dict[str, Any]]:
    if include_automated:
        return list(sessions)
    return [session for session in sessions if not is_automated(session)]


def _one_liners_by_id(rows: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        sid = row.get("session_id")
        payload = row.get("payload")
        if not isinstance(sid, str) or not isinstance(payload, dict):
            continue
        one_liner = payload.get("one_liner")
        if isinstance(one_liner, str) and one_liner.strip():
            out[sid] = one_liner.strip()
    return out


def build_library_home_context(
    index: Mapping[str, Any],
    output_dir: Path,
    *,
    now: Optional[datetime] = None,
    include_automated: bool = False,
) -> dict[str, Any]:
    """Build continue / projects / recent-days blocks from a cached index payload."""
    sessions = _visible_sessions(
        list(index.get("sessions") or []), include_automated=include_automated
    )
    ref = now or datetime.now()

    sorted_sessions = sorted(sessions, key=_sort_stamp, reverse=True)
    continue_raw = sorted_sessions[:CONTINUE_LIMIT]

    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        key = str(session.get("project") or "").strip() or "Unassigned"
        by_project[key].append(session)

    project_rows: list[dict[str, Any]] = []
    for path, group in by_project.items():
        newest = max(group, key=_sort_stamp)
        project_rows.append(
            {
                "path": path,
                "label": short_project_label(path if path != "Unassigned" else ""),
                "session_count": len(group),
                "last_active": _sort_stamp(newest)[:10],
                "url": project_page_url(path),
                "newest_title": _display_title(newest),
            }
        )
    project_rows.sort(key=lambda row: row.get("last_active") or "", reverse=True)
    project_rows = project_rows[:PROJECTS_LIMIT]

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        day = session_day_iso(session)
        if day:
            by_day[day].append(session)

    recent_days: list[dict[str, Any]] = []
    for day in sorted(by_day.keys(), reverse=True)[:RECENT_DAYS_LIMIT]:
        day_sessions = sorted(by_day[day], key=_sort_stamp, reverse=True)
        recent_days.append(
            {
                "date": day,
                "session_count": len(day_sessions),
                "sessions": [
                    {
                        "id": session.get("id"),
                        "title": _display_title(session),
                    }
                    for session in day_sessions[:DAY_TITLE_LIMIT]
                ],
            }
        )

    summary_ids: set[str] = set()
    for session in continue_raw:
        sid = session.get("id")
        if isinstance(sid, str) and sid:
            summary_ids.add(sid)

    rows = list_summaries(output_dir, level="session", session_ids=sorted(summary_ids))
    one_liners = _one_liners_by_id(rows)

    library_continue: list[dict[str, Any]] = []
    for session in continue_raw:
        sid = str(session.get("id") or "")
        stamp = session.get("updated") or session.get("created")
        library_continue.append(
            {
                "id": sid,
                "title": _display_title(session),
                "project_label": short_project_label(session.get("project")),
                "tool": str(session.get("tool") or ""),
                "activity_time": format_relative_activity_time(str(stamp or ""), now=ref),
                "summary": one_liners.get(sid),
            }
        )

    return {
        "library_continue": library_continue,
        "library_projects": project_rows,
        "library_recent_days": recent_days,
    }
