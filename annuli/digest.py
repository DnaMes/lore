"""Activity digest — a periodic summary of recent AI coding sessions.

Powers the ``lore digest`` CLI command (issue #43). The logic here is
deliberately pure: it takes already-loaded index session records plus a
cutoff datetime and returns a structured :class:`Digest`, with no disk or
clock access. The CLI layer handles index loading, ``--since`` parsing and
output formatting.

Note: the flat ``index.json`` records carry message/prompt counts but no
token or cost data, so the digest reports activity counts rather than spend.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .utils.datetime import make_naive, parse_timestamp

logger = logging.getLogger(__name__)


@dataclass
class Digest:
    """Structured summary of session activity within a time window."""

    since: datetime
    until: datetime
    total_sessions: int = 0
    total_messages: int = 0
    total_prompts: int = 0
    by_day: Dict[str, int] = field(default_factory=dict)
    by_tool: Dict[str, int] = field(default_factory=dict)
    top_projects: List[tuple[str, int]] = field(default_factory=list)
    busiest_day: Optional[tuple[str, int]] = None

    def is_empty(self) -> bool:
        return self.total_sessions == 0


def _session_time(record: Dict[str, Any]) -> Optional[datetime]:
    """Return a naive datetime for a session record, or None if unparseable."""
    raw = record.get("updated") or record.get("created")
    if not raw:
        return None
    try:
        return make_naive(parse_timestamp(raw))
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        # Malformed timestamps skip the session — but anything else (a bug
        # in make_naive itself) must not silently disappear (#122).
        logger.debug("Skipping unparseable session timestamp %r: %s", raw, exc)
        return None


def build_digest(
    sessions: Sequence[Dict[str, Any]],
    since: datetime,
    until: Optional[datetime] = None,
    top_n_projects: int = 5,
) -> Digest:
    """Aggregate index session records into a :class:`Digest`.

    Args:
        sessions: index session records (as stored in ``index.json``).
        since: lower bound (inclusive); sessions older than this are skipped.
        until: upper bound (inclusive); defaults to ``datetime.now()``.
        top_n_projects: how many projects to keep in ``top_projects``.
    """
    if until is None:
        until = datetime.now()

    since_naive = make_naive(since)
    until_naive = make_naive(until)

    digest = Digest(since=since_naive, until=until_naive)
    by_day: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    by_project: Counter[str] = Counter()

    for record in sessions:
        when = _session_time(record)
        if when is None or when < since_naive or when > until_naive:
            continue

        digest.total_sessions += 1
        digest.total_messages += int(record.get("messages") or 0)
        digest.total_prompts += int(record.get("prompts") or 0)

        by_day[when.strftime("%Y-%m-%d")] += 1
        by_tool[str(record.get("tool") or "unknown")] += 1

        project = record.get("project")
        if project:
            by_project[str(project)] += 1

    # Sort by_day chronologically; by_tool and projects by descending count.
    digest.by_day = dict(sorted(by_day.items()))
    digest.by_tool = dict(by_tool.most_common())
    digest.top_projects = by_project.most_common(top_n_projects)
    if by_day:
        digest.busiest_day = by_day.most_common(1)[0]

    return digest


def _project_label(path: str) -> str:
    """Shorten an absolute project path to its trailing directory name."""
    cleaned = path.rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or cleaned or path


def format_digest(digest: Digest, fmt: str = "text") -> str:
    """Render a :class:`Digest` as plain text or Markdown."""
    if fmt not in ("text", "markdown"):
        raise ValueError(f"Unknown digest format: {fmt!r}")

    window = f"{digest.since:%Y-%m-%d} → {digest.until:%Y-%m-%d}"

    if digest.is_empty():
        if fmt == "markdown":
            return f"# AI History Digest\n\n_{window}_\n\nNo sessions in this window.\n"
        return f"AI History Digest ({window})\n\nNo sessions in this window."

    md = fmt == "markdown"
    lines: List[str] = []

    if md:
        lines.append("# AI History Digest")
        lines.append("")
        lines.append(f"_{window}_")
        lines.append("")
        lines.append(f"- **Sessions:** {digest.total_sessions}")
        lines.append(f"- **Messages:** {digest.total_messages}")
        lines.append(f"- **Prompts:** {digest.total_prompts}")
        if digest.busiest_day:
            day, count = digest.busiest_day
            lines.append(f"- **Busiest day:** {day} ({count} sessions)")
        lines.append("")
        lines.append("## Sessions by day")
        lines.append("")
        for day, count in digest.by_day.items():
            lines.append(f"- `{day}` — {count}")
        lines.append("")
        lines.append("## By tool")
        lines.append("")
        for tool, count in digest.by_tool.items():
            lines.append(f"- **{tool}** — {count}")
        lines.append("")
        lines.append("## Top projects")
        lines.append("")
        for path, count in digest.top_projects:
            lines.append(f"- {_project_label(path)} — {count}")
        lines.append("")
        return "\n".join(lines)

    # Plain text
    lines.append(f"AI History Digest ({window})")
    lines.append("=" * 48)
    lines.append(f"Sessions : {digest.total_sessions}")
    lines.append(f"Messages : {digest.total_messages}")
    lines.append(f"Prompts  : {digest.total_prompts}")
    if digest.busiest_day:
        day, count = digest.busiest_day
        lines.append(f"Busiest  : {day} ({count} sessions)")
    lines.append("")
    lines.append("Sessions by day:")
    for day, count in digest.by_day.items():
        bar = "█" * min(count, 40)
        lines.append(f"  {day}  {count:>3}  {bar}")
    lines.append("")
    lines.append("By tool:")
    for tool, count in digest.by_tool.items():
        lines.append(f"  {tool:<16} {count:>4}")
    lines.append("")
    lines.append("Top projects:")
    for path, count in digest.top_projects:
        lines.append(f"  {_project_label(path):<24} {count:>4}")
    return "\n".join(lines)
