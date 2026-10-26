"""Data loading and caching for the web interface.

This module handles session loading, index management and data caching
for the web interface.

Most of the genuinely shared, framework-free logic now lives in
``lore.services`` (issue #47). The functions here are thin
wrappers that supply this module's — test-patchable — path globals
(``INDEX_PATH``, ``OUTPUT_DIR``, ``DELETED_SESSIONS_PATH``) to the
service-layer functions, plus the Flask-specific bits that stay here
(export-markdown parsing, export-path resolution, the sessions cache).

Re-exports from ``lore.services`` keep the historical
``web_data.<symbol>`` import surface intact for callers and tests.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services import (
    ActionJobCancelledError,  # noqa: F401  (backwards-compat re-export)
    annotate_display_titles,
    apply_deleted_filter,
    build_search_index,
    collect_sessions,
    threadsafe_lru_cache,
)
from lore.services import (
    clear_index_cache as _service_clear_index_cache,
)
from lore.services import (
    load_index as _service_load_index,
)
from lore.services import (
    load_index_summary as _service_load_index_summary,
)
from lore.services import (
    remember_deleted_session_id as _service_remember_deleted_session_id,
)
from lore.services import (
    save_deleted_session_ids as _service_save_deleted_session_ids,
)
from lore.services import (
    search_index as _service_search_index,
)
from lore.services.index import (
    _load_deleted_session_ids_cached,
    _load_index_cached,
    _load_index_v2_cached,
)
from lore.services.index import (
    load_deleted_session_ids as _service_load_deleted_session_ids,
)
from lore.utils.paths import lore_home

# Backwards-compatible re-export — same signature as the service helper.
_annotate_display_titles = annotate_display_titles


def _apply_deleted_filter(payload: dict) -> dict:
    """Backwards-compatible wrapper: reads the tombstone set from this
    module's ``DELETED_SESSIONS_PATH`` then delegates to the service helper.
    """
    return apply_deleted_filter(payload, load_deleted_session_ids())


# Paths — kept as module globals so tests can monkeypatch them. The
# service-layer functions take paths as explicit arguments; the wrappers
# below pass these values, so patching them here stays effective.
OUTPUT_DIR = lore_home()
INDEX_PATH = OUTPUT_DIR / "index.json"
DELETED_SESSIONS_PATH = OUTPUT_DIR / "deleted_sessions.json"

# Clear OpenCode cache if requested
if os.environ.get("LORE_CLEAR_OPENCODE_CACHE", "").lower() == "true":
    opencode_state_cache = OUTPUT_DIR / "cache" / "opencode_state.json"
    if opencode_state_cache.exists():
        opencode_state_cache.unlink()


# --- Deleted Session IDs ---


def load_deleted_session_ids() -> set[str]:
    return _service_load_deleted_session_ids(DELETED_SESSIONS_PATH)


def remember_deleted_session_id(session_id: str) -> None:
    _service_remember_deleted_session_id(session_id, OUTPUT_DIR, DELETED_SESSIONS_PATH)


def _save_deleted_session_ids(session_ids: set[str]) -> None:
    _service_save_deleted_session_ids(session_ids, OUTPUT_DIR, DELETED_SESSIONS_PATH)


# --- URL Sanitization ---


def _sanitize_next_url(next_url: str) -> str:
    from urllib.parse import urlparse

    value = (next_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return ""
    if not value.startswith("/") or value.startswith("//"):
        return ""
    return value


# --- Cache Management ---


def clear_index_cache():
    _service_clear_index_cache()
    load_export_lookup.cache_clear()


def clear_sessions_cache():
    load_sessions_for_tool.cache_clear()


# --- Index Building ---


def _build_index_from_extractors(
    tool_filter: Optional[str] = None,
    progress_callback=None,
    should_stop=None,
    incremental: bool = True,
):
    """Build the search index (wrapper over ``services.build_search_index``).

    When ``incremental=True`` (default) sessions already present in the
    existing index whose source-file mtime hasn't changed are reused
    verbatim. Set ``incremental=False`` to force a full rebuild.
    """
    return build_search_index(
        OUTPUT_DIR,
        INDEX_PATH,
        deleted_ids=load_deleted_session_ids(),
        tool_filter=tool_filter,
        progress_callback=progress_callback,
        should_stop=should_stop,
        incremental=incremental,
    )


@threadsafe_lru_cache(maxsize=128)
def load_sessions_for_tool(tool: Optional[str] = None):
    return collect_sessions(tool, deleted_ids=load_deleted_session_ids())


# --- Index Loading & Search ---


def search_index(
    query,
    tool=None,
    project=None,
    limit=50,
    scope=None,
    include_automated=False,
    tag=None,
    start=None,
    end=None,
    offset=0,
):
    """Search sessions (wrapper over ``services.search_index``)."""
    return _service_search_index(
        INDEX_PATH,
        query,
        load_deleted_session_ids(),
        tool=tool,
        project=project,
        limit=limit,
        scope=scope,
        include_automated=include_automated,
        tag=tag,
        start=start,
        end=end,
        offset=offset,
    )


def load_index():
    return _service_load_index(INDEX_PATH, OUTPUT_DIR, load_deleted_session_ids())


def load_index_summary(include_automated: bool = False) -> dict:
    """Return lightweight index metadata without loading full session details."""
    return _service_load_index_summary(
        INDEX_PATH, load_deleted_session_ids(), include_automated=include_automated
    )


# --- Export Path Resolution ---


def resolve_export_path(path_value: Optional[str]) -> Optional[Path]:
    if not path_value:
        return None

    def _safe(candidate: Path) -> Optional[Path]:
        try:
            resolved = candidate.resolve()
            if resolved.is_relative_to(OUTPUT_DIR.resolve()) and resolved.exists():
                return resolved
        except (ValueError, OSError):
            pass
        return None

    raw_path = Path(path_value)
    result = _safe(raw_path)
    if result:
        return result

    return None


@threadsafe_lru_cache(maxsize=1)
def load_export_lookup():
    mapping: dict[str, Path] = {}
    projects_dir = OUTPUT_DIR / "projects"
    if not projects_dir.exists():
        return mapping

    for md_file in projects_dir.rglob("*.md"):
        try:
            header = md_file.read_text(encoding="utf-8", errors="replace")[:500]
        except OSError:
            continue
        match = re.search(r"session_id:\s*(\S+)", header)
        if match:
            mapping[match.group(1)] = md_file
    return mapping


# --- Session Building ---


def _tool_from_value(value: Optional[str]) -> Tool:
    try:
        return Tool(value or "")
    except ValueError:
        return Tool.OPENCODE


def build_session_from_export_markdown(
    session_id: str, session_meta: dict, export_path: Path
) -> Optional[UnifiedSession]:
    try:
        text = export_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    title_match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else session_meta.get("title")
    if title and (title.lower().startswith("session ") or title.lower().startswith("chat ")):
        title = None

    created_raw = session_meta.get("created")
    updated_raw = session_meta.get("updated")
    try:
        created_at = (
            datetime.fromisoformat(created_raw) if created_raw else datetime.fromtimestamp(0)
        )
    except ValueError:
        created_at = datetime.fromtimestamp(0)
    try:
        updated_at = datetime.fromisoformat(updated_raw) if updated_raw else created_at
    except ValueError:
        updated_at = created_at

    conversation_anchor = text.find("## Conversation")
    if conversation_anchor == -1:
        return None
    convo = text[conversation_anchor:]

    pattern = re.compile(
        r"^###\s+(User|Assistant|System|Tool|Info)\s+\((\d{2}:\d{2}:\d{2})\)\s*$",
        flags=re.MULTILINE,
    )
    matches = list(pattern.finditer(convo))
    if not matches:
        return None

    messages: list[UnifiedMessage] = []
    role_map = {
        "User": Role.USER,
        "Assistant": Role.ASSISTANT,
        "System": Role.SYSTEM,
        "Tool": Role.TOOL,
        "Info": Role.INFO,
    }

    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(convo)
        block = convo[start:end]
        lines = []
        for raw in block.splitlines():
            if raw.strip() == "---":
                continue
            if raw.startswith("> "):
                lines.append(raw[2:])
            else:
                lines.append(raw)
        content = "\n".join(lines).strip()
        if not content:
            continue

        time_part = match.group(2)
        try:
            msg_time = datetime.fromisoformat(f"{created_at.date().isoformat()}T{time_part}")
        except ValueError:
            msg_time = created_at

        messages.append(
            UnifiedMessage(
                role=role_map.get(match.group(1), Role.INFO),
                content=content,
                timestamp=msg_time,
            )
        )

    if not messages:
        return None

    return UnifiedSession(
        tool=_tool_from_value(session_meta.get("tool")),
        session_id=session_id,
        created_at=created_at,
        last_updated=updated_at,
        messages=messages,
        project_path=session_meta.get("project"),
        thread_id=session_meta.get("thread_id"),
        title=title,
        source_path=str(export_path),
    )


# Re-bound for ``clear_index_cache`` and tests that historically reached
# ``web_data._load_*_cached.cache_clear()``. These are the *same* cache
# objects the service layer uses, so clearing either clears both.
__all__ = [
    "OUTPUT_DIR",
    "INDEX_PATH",
    "DELETED_SESSIONS_PATH",
    "ActionJobCancelledError",
    "threadsafe_lru_cache",
    "load_deleted_session_ids",
    "remember_deleted_session_id",
    "_save_deleted_session_ids",
    "_load_deleted_session_ids_cached",
    "_load_index_cached",
    "_load_index_v2_cached",
    "_sanitize_next_url",
    "_apply_deleted_filter",
    "_annotate_display_titles",
    "clear_index_cache",
    "clear_sessions_cache",
    "_build_index_from_extractors",
    "load_sessions_for_tool",
    "load_index",
    "load_index_summary",
    "search_index",
    "resolve_export_path",
    "load_export_lookup",
    "_tool_from_value",
    "build_session_from_export_markdown",
]
