"""Select memories for agent context blocks (metadata ranking only, no embeddings).

Project tiers: 0 = exact ``scope_project``, 1 = related path (descendant scope always;
ancestor scope only when specific enough), 2 = global (no ``scope_project``).
Ancestor scopes equal to ``/``, ``Path.home()``, or with ``len(path.parts) < 4`` are
excluded so home-wide or filesystem-root memories do not attach to every project.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from lore.services.improve import normalize_scope_project
from lore.storage.memory import MemoryEntry, list_memory
from lore.utils.redact import redact

_RETIRED_MARKER_RE = re.compile(r"\b(OBSOLETE|SUPERSEDED|RETIRED)\b", re.IGNORECASE)
_KIND_PRIORITY: dict[str, int] = {
    "lesson": 0,
    "decision": 1,
    "fact": 2,
    "link": 3,
}
_DEFAULT_KIND_PRIORITY = 99
_LIST_CAP = 10_000


def _resolve_project_path(scope_project: str | Path | None) -> Path | None:
    if scope_project is None:
        return None
    if isinstance(scope_project, Path):
        return scope_project.expanduser().resolve()
    normalized = normalize_scope_project(str(scope_project))
    if not normalized:
        return None
    return Path(normalized).expanduser().resolve()


def _ancestor_scope_too_broad(scope: Path) -> bool:
    if scope == Path("/"):
        return True
    if scope == Path.home():
        return True
    return len(scope.parts) < 4


def _paths_related(project: Path, scope_project: str) -> bool:
    try:
        other = Path(scope_project).expanduser().resolve()
    except OSError:
        return False
    if project == other:
        return True
    try:
        if other.is_relative_to(project):
            return True
        if project.is_relative_to(other):
            return not _ancestor_scope_too_broad(other)
        return False
    except ValueError:
        return False


def _project_tier(entry: MemoryEntry, project: Path | None) -> int | None:
    """0 = exact project, 1 = related project, 2 = global; None = excluded."""
    if project is None:
        return 2 if entry.scope_project is None else None
    if entry.scope_project is None:
        return 2
    if not _paths_related(project, entry.scope_project):
        return None
    return 0 if Path(entry.scope_project).expanduser().resolve() == project else 1


def _tool_matches(entry: MemoryEntry, scope_tool: str | None) -> bool:
    if scope_tool is None:
        return True
    return entry.scope_tool is None or entry.scope_tool == scope_tool


def _has_retired_marker(entry: MemoryEntry) -> bool:
    return _RETIRED_MARKER_RE.search(f"{entry.title}\n{entry.body}") is not None


def _rank_key(entry: MemoryEntry, tier: int) -> tuple[float, float, str, float, int]:
    kind_rank = _KIND_PRIORITY.get(entry.kind, _DEFAULT_KIND_PRIORITY)
    return (-float(tier), entry.feedback_weight, entry.updated, -float(kind_rank), entry.id)


def select_context_memories(
    output_dir: Path,
    *,
    scope_project: str | Path | None = None,
    scope_tool: str | None = None,
    limit: int = 40,
) -> list[MemoryEntry]:
    """Return confirmed, active memories ranked for injection into a context block."""
    project = _resolve_project_path(scope_project)
    capped = max(1, limit)
    rows = list_memory(
        output_dir,
        include_superseded=False,
        limit=_LIST_CAP,
        status="confirmed",
    )
    eligible: list[tuple[MemoryEntry, int]] = []
    for entry in rows:
        if _has_retired_marker(entry):
            continue
        if not _tool_matches(entry, scope_tool):
            continue
        tier = _project_tier(entry, project)
        if tier is None:
            continue
        eligible.append((entry, tier))

    eligible.sort(key=lambda pair: _rank_key(pair[0], pair[1]), reverse=True)
    return [entry for entry, _tier in eligible[:capped]]


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_FIRST_SENTENCE_MAX_LEN = 160
_ELLIPSIS = "…"
_STRAY_END_RE = re.compile(r"[\*`_]+$")


def _normalize_one_line(text: str) -> str:
    """Collapse whitespace, strip light markdown, unescape quotes and HTML entities."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if not collapsed:
        return ""
    without_md = re.sub(r"\*\*|__|`", "", collapsed)
    without_md = re.sub(r"^(?:[-#]\s*)+", "", without_md)
    unescaped = without_md.replace('\\"', '"')
    return html.unescape(unescaped).strip()


def _take_first_sentence(text: str) -> str:
    """First sentence: split on '. ', '! ', '? ' only before uppercase or end."""
    normalized = _normalize_one_line(text)
    if not normalized:
        return ""
    candidate = normalized
    n = len(normalized)
    for i, ch in enumerate(normalized):
        if ch not in ".!?":
            continue
        if i + 1 == n:
            candidate = normalized[: i + 1]
            break
        if normalized[i + 1] != " ":
            continue
        after = normalized[i + 2 :]
        if not after or after[0].isupper():
            candidate = normalized[: i + 1]
            break
    candidate = _STRAY_END_RE.sub("", candidate.strip())
    return candidate.strip()


def _cap_words(text: str, max_len: int = _FIRST_SENTENCE_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    limit = max_len - len(_ELLIPSIS)
    if limit < 1:
        return _ELLIPSIS[:max_len]
    chunk = text[:limit]
    last_space = chunk.rfind(" ")
    if last_space > 0:
        chunk = chunk[:last_space]
    return chunk.rstrip() + _ELLIPSIS


def _first_sentence(body: str, max_len: int = _FIRST_SENTENCE_MAX_LEN) -> str:
    """Return the first sentence of ``body``, capped at ``max_len`` characters."""
    # Bodies start with a one-paragraph description that often lacks a final period.
    first_paragraph = re.split(r"\n\s*\n", body.strip(), maxsplit=1)[0]
    sentence = _take_first_sentence(first_paragraph)
    if not sentence:
        return ""
    return _cap_words(sentence, max_len)


def _normalize_title(title: str) -> str:
    return _normalize_one_line(title)


def _format_context_line(entry: MemoryEntry) -> str:
    snippet = _first_sentence(entry.body)
    title = _normalize_title(entry.title)
    return f"- [{entry.kind} #{entry.id}] {title}: {snippet}"


def _token_estimate(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0


def _more_line(cut: int) -> str:
    return f'({cut} more; recall with: lore memory search "<terms>" or lore memory show <id>)'


def _entry_record(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "title": _normalize_title(entry.title),
        "first_sentence": _first_sentence(entry.body),
        "project": entry.scope_project,
    }


def _sanitize_free_text(text: str) -> str:
    cleaned = redact(text).text
    return _UUID_RE.sub("<uuid>", cleaned)


def _truncate_line_to_tokens(line: str, max_tokens: int) -> str:
    if _token_estimate(line) <= max_tokens:
        return line
    max_chars = max(1, max_tokens * 4)
    trimmed = line[:max_chars]
    if len(trimmed) < len(line):
        trimmed = trimmed.rstrip()
        if len(trimmed) + len(_ELLIPSIS) <= max_chars:
            trimmed += _ELLIPSIS
        else:
            trimmed = trimmed[: max(1, max_chars - len(_ELLIPSIS))].rstrip() + _ELLIPSIS
    while trimmed and _token_estimate(trimmed) > max_tokens:
        trimmed = trimmed[:-1]
    return trimmed


def _fit_context_lines(entries: list[MemoryEntry], budget: int) -> tuple[list[str], int]:
    """Return rendered, redacted lines that fit ``budget`` (token estimate) and the cut count.

    Redaction runs before truncation so a secret cannot be split into a partial token.
    """
    capped_budget = max(1, budget)
    lines: list[str] = []
    used_tokens = 0
    for entry in entries:
        line = _sanitize_free_text(_format_context_line(entry))
        prefix = "\n" if lines else ""
        next_tokens = _token_estimate(prefix + line)
        if lines and used_tokens + next_tokens > capped_budget:
            break
        if not lines and next_tokens > capped_budget:
            # The first entry is forced in; truncate it so it fits the budget.
            line = _truncate_line_to_tokens(line, capped_budget)
            if not line:
                break
            lines.append(line)
            break
        lines.append(line)
        used_tokens += next_tokens
    cut = len(entries) - len(lines)
    return lines, cut


def render_context_memories(
    entries: list[MemoryEntry],
    *,
    budget: int = 2000,
    output_format: str = "text",
) -> str:
    """Render selected memories for injection; empty string when nothing to show."""
    if not entries:
        return ""

    if output_format == "json":
        return _render_json(entries, budget)

    capped = max(1, budget)
    lines, cut = _fit_context_lines(entries, capped)
    if not cut:
        return "\n".join(lines)
    # Reserve footer room at its longest possible length (cut <= len(entries)) so the
    # refit below cannot grow the footer.
    reserve = _token_estimate("\n" + _more_line(len(entries)))
    if capped <= reserve:
        # Budget too small for the footer too: the budget wins, so drop the footer.
        return "\n".join(lines)
    lines, cut = _fit_context_lines(entries, capped - reserve)
    return "\n".join([*lines, _more_line(cut)])


def _render_json(entries: list[MemoryEntry], budget: int) -> str:
    """Drop trailing entries until the serialized JSON fits the token budget."""
    capped = max(1, budget)
    for count in range(len(entries), 0, -1):
        payload = {
            "entries": [
                {
                    **rec,
                    "title": _sanitize_free_text(rec["title"]),
                    "first_sentence": _sanitize_free_text(rec["first_sentence"]),
                }
                for rec in (_entry_record(e) for e in entries[:count])
            ],
            "cut": len(entries) - count,
        }
        raw = json.dumps(payload, indent=2, ensure_ascii=False)
        if _token_estimate(raw) <= capped:
            return raw
    return ""


def build_memory_context(
    output_dir: Path,
    *,
    scope_project: str | Path | None = None,
    scope_tool: str | None = None,
    limit: int = 40,
    budget: int = 2000,
    output_format: str = "text",
) -> str:
    """Select and render memories; never raises (safe for session hooks)."""
    try:
        entries = select_context_memories(
            output_dir,
            scope_project=scope_project,
            scope_tool=scope_tool,
            limit=limit,
        )
        return render_context_memories(entries, budget=budget, output_format=output_format)
    except Exception as exc:  # noqa: BLE001 - hooks must not fail on missing or broken stores
        if os.environ.get("LORE_DEBUG", "").strip() == "1":
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return ""
