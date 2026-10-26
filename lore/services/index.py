"""Framework-free index reading, searching and deleted-session handling.

These are the pure (no Flask, no interface imports) entry points that
both the web interface and the MCP server consume. ``web_data`` wraps
them, passing its own — test-patchable — path globals.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from lore.core.models import AUTOMATED_ORIGINS

from .cache import threadsafe_lru_cache
from .extraction import build_search_index

logger = logging.getLogger(__name__)


# --- Deleted Session IDs ---


@threadsafe_lru_cache(maxsize=1)
def _load_deleted_session_ids_cached(path: str, mtime_ns: int, size: int) -> set[str]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        values = payload.get("session_ids", [])
    else:
        values = payload
    return {str(value) for value in values if str(value).strip()}


def load_deleted_session_ids(deleted_sessions_path: Path) -> set[str]:
    """Load the set of tombstoned session ids from ``deleted_sessions_path``."""
    if not deleted_sessions_path.exists():
        return set()
    stat = deleted_sessions_path.stat()
    try:
        return set(
            _load_deleted_session_ids_cached(
                str(deleted_sessions_path), stat.st_mtime_ns, stat.st_size
            )
        )
    except Exception as exc:
        logger.warning("Failed to load deleted session ids from %s: %s", deleted_sessions_path, exc)
        return set()


def save_deleted_session_ids(
    session_ids: set[str], output_dir: Path, deleted_sessions_path: Path
) -> None:
    """Persist ``session_ids`` to ``deleted_sessions_path`` (owner-only, #41)."""
    from lore.utils.paths import restrict_file, secure_dir

    secure_dir(output_dir)
    with open(deleted_sessions_path, "w", encoding="utf-8") as handle:
        json.dump({"session_ids": sorted(session_ids)}, handle, indent=2)
    restrict_file(deleted_sessions_path)
    _load_deleted_session_ids_cached.cache_clear()


def remember_deleted_session_id(
    session_id: str, output_dir: Path, deleted_sessions_path: Path
) -> None:
    """Add ``session_id`` to the tombstone set and persist it."""
    ids = load_deleted_session_ids(deleted_sessions_path)
    ids.add(session_id)
    save_deleted_session_ids(ids, output_dir, deleted_sessions_path)
    from lore.storage.knowledge import delete_session_knowledge
    from lore.storage.summaries import delete_summaries_for_session

    # Each purge is attempted on its own; a failure is logged, never blocks the tombstone
    # (the tombstone alone already hides the rows from every reader).
    for purge in (delete_summaries_for_session, delete_session_knowledge):
        try:
            purge(output_dir, session_id)
        except Exception as exc:  # noqa: BLE001 - a broken store must not undo the deletion
            logger.warning(
                "Failed to purge %s for deleted session %s: %s", purge.__name__, session_id, exc
            )


# --- Index Filtering ---


def _recompute_stats(sessions: list[dict]) -> dict:
    by_tool: dict[str, int] = {}
    by_project: dict[str, int] = {}
    total_messages = 0
    for session in sessions:
        tool = str(session.get("tool") or "")
        if tool:
            by_tool[tool] = by_tool.get(tool, 0) + 1
        project = session.get("project")
        if project:
            by_project[project] = by_project.get(project, 0) + 1
        total_messages += int(session.get("messages") or 0)
    return {
        "total_sessions": len(sessions),
        "total_messages": total_messages,
        "by_tool": by_tool,
        "by_project": by_project,
    }


def apply_deleted_filter(payload: dict, deleted: set[str]) -> dict:
    """Drop tombstoned sessions from an index payload and recompute stats."""
    if not deleted:
        return payload

    sessions = [
        session for session in payload.get("sessions", []) if session.get("id") not in deleted
    ]
    payload["sessions"] = sessions
    payload["stats"] = _recompute_stats(sessions)
    return payload


# --- Automated sessions (origin policy, #197) ---


def _origin_of(session: Any) -> str:
    """Origin of an index dict or a ``UnifiedSession``; missing means not automated."""
    if isinstance(session, dict):
        return str(session.get("origin") or "")
    return str(getattr(session, "origin", "") or "")


class InvalidFlagError(ValueError):
    """A boolean flag argument that is neither a boolean nor a known string."""


_TRUE_FLAGS = frozenset({"true", "1", "yes", "on"})
_FALSE_FLAGS = frozenset({"false", "0", "no", "off", ""})


def parse_bool_flag(value: Any, default: bool = False, name: str = "flag") -> bool:
    """Strict boolean for MCP arguments and query parameters.

    ``bool("false")`` is True, so strings are parsed by name. ``None`` gives ``default``;
    anything that is not a bool, 0/1 or a known string raises ``InvalidFlagError``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_FLAGS:
            return True
        if text in _FALSE_FLAGS:
            return False
    raise InvalidFlagError(f"Invalid {name} parameter.")


def is_automated(session: Any) -> bool:
    return _origin_of(session) in AUTOMATED_ORIGINS


class _IdentityMemo:
    """Small bounded memo keyed by the identity of a source object.

    The source is held alive next to its result, so ``id()`` cannot be reused and the
    ``is`` check stays valid. Callers pass lists/payloads that are shared, cached objects
    and never mutated in place; an index rebuild or tombstone change yields a new object.
    """

    def __init__(self, slots: int = 4) -> None:
        self._slots = slots
        self._lock = threading.Lock()
        self._items: OrderedDict[int, tuple[Any, Any]] = OrderedDict()

    def get(self, source: Any) -> Optional[Any]:
        with self._lock:
            hit = self._items.get(id(source))
        if hit is not None and hit[0] is source:
            return hit[1]
        return None

    def put(self, source: Any, value: Any) -> None:
        with self._lock:
            self._items[id(source)] = (source, value)
            self._items.move_to_end(id(source))
            while len(self._items) > self._slots:
                self._items.popitem(last=False)


# Memos of the default (hiding) view. Results are returned by identity so the
# ``thread_overview_by_id`` / ``session_by_id_map`` memos downstream keep hitting (#126).
_VISIBLE_LIST_MEMO = _IdentityMemo()
_VISIBLE_PAYLOAD_MEMO = _IdentityMemo()


def filter_automated(sessions: Any, include_automated: bool = False) -> Any:
    """Hide non-interactive sessions (``exec``/``cron``/``subagent``) unless asked for.

    The one place every list view and LLM stage goes through. Works on index dicts
    and ``UnifiedSession`` objects; the list is returned unchanged when nothing is hidden.
    The hiding result is memoized on the identity of ``sessions``: the same list object
    always yields the same result object.
    """
    if include_automated:
        return sessions
    cached = _VISIBLE_LIST_MEMO.get(sessions)
    if cached is not None:
        return cached
    if any(is_automated(s) for s in sessions):
        visible: Any = [s for s in sessions if not is_automated(s)]
    else:
        visible = sessions
    _VISIBLE_LIST_MEMO.put(sessions, visible)
    return visible


def filter_automated_payload(payload: dict, include_automated: bool = False) -> dict:
    """Index payload without automated sessions, stats recomputed, ``automated_hidden`` set.

    Returns ``payload`` itself when nothing is hidden. Never mutates ``payload`` (it
    may be a shared cached object). Independent of the tombstone filter's order.
    Memoized on the identity of ``payload`` and of its ``sessions`` list, so repeated
    calls on one cached payload return one object.
    """
    if include_automated:
        return payload
    sessions = payload.get("sessions", [])
    cached = _VISIBLE_PAYLOAD_MEMO.get(payload)
    if cached is not None and cached[0] is sessions:
        return cached[1]
    visible = filter_automated(sessions, False)
    if visible is sessions:
        out = payload
    else:
        out = dict(payload)
        out["sessions"] = visible
        out["stats"] = _recompute_stats(visible)
        out["automated_hidden"] = len(sessions) - len(visible)
    _VISIBLE_PAYLOAD_MEMO.put(payload, (sessions, out))
    return out


def thread_sessions(sessions: list[dict], thread_id: str, include_automated: bool = False) -> list:
    """Sessions a thread view works on.

    A thread that holds only automated sessions stays reachable by its id: the
    unfiltered list is returned then, so a known thread id never turns into a 404.
    """
    visible = filter_automated(sessions, include_automated)
    if visible is not sessions and not any(s.get("thread_id") == thread_id for s in visible):
        return sessions
    return visible


def thread_hidden_count(
    sessions: list[dict], thread_id: str, include_automated: bool = False
) -> int:
    """Automated members of ``thread_id`` that ``thread_sessions`` leaves out."""
    if include_automated:
        return 0
    shown = thread_sessions(sessions, thread_id, False)
    members = sum(1 for s in sessions if s.get("thread_id") == thread_id)
    return members - sum(1 for s in shown if s.get("thread_id") == thread_id)


def filter_automated_results(results: list, include_automated: bool = False) -> list:
    """Drop search results of automated sessions (rows carry ``session.origin``)."""
    if include_automated:
        return results
    return [r for r in results if not is_automated(r.get("session", {}))]


def automated_hidden_line(count: int) -> str:
    """ "12 automated sessions hidden", or an empty string when nothing is hidden."""
    if count <= 0:
        return ""
    return f"{count} automated session{'' if count == 1 else 's'} hidden"


def apply_deleted_filter_to_results(results: list, deleted: set[str]) -> list:
    """Drop search results whose session id is tombstoned."""
    if not deleted:
        return results
    return [r for r in results if r.get("session", {}).get("id") not in deleted]


# --- Display Titles ---


def annotate_display_titles(sessions: list[dict]) -> list[dict]:
    """Add a ``display_title`` to each session, disambiguating duplicates."""
    title_counts: dict[str, int] = {}
    for session in sessions:
        raw_title = (session.get("title") or "").strip()
        if not raw_title:
            continue
        key = raw_title.casefold()
        title_counts[key] = title_counts.get(key, 0) + 1

    for session in sessions:
        raw_title = (session.get("title") or "").strip()
        session_id = str(session.get("id") or "")
        if raw_title:
            key = raw_title.casefold()
            if title_counts.get(key, 0) > 1:
                suffix = session_id[:8] if session_id else "session"
                session["display_title"] = f"{raw_title} · {suffix}"
            else:
                session["display_title"] = raw_title
        else:
            session["display_title"] = session_id[:12] if session_id else "Untitled Session"
    return sessions


# --- Cache Management ---


@threadsafe_lru_cache(maxsize=1)
def _load_index_cached(index_path: str, _mtime_ns: int, _size: int) -> dict:
    with open(index_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@threadsafe_lru_cache(maxsize=1)
def _load_index_v2_cached(
    db_path: str,
    _mtime_ns: int,
    _size: int,
    _ignored_mtime_ns: int,
    _ignored_size: int,
) -> dict:
    """File-stat-keyed cache of the v2 store read (mirrors _load_index_cached).

    ``ignored.json`` stats are part of the key: prune/un-prune only rewrites
    that file, so DB mtime/size alone would serve a stale filtered list.
    """
    from lore.storage import load_index_v2

    return load_index_v2(Path(db_path).parent)


def _finalize_index_payload(payload: dict, deleted: frozenset[str]) -> dict:
    """Apply the tombstone filter + display titles to a raw index payload.

    With no tombstones the shared cached payload is returned as-is (the
    historical behavior) — ``annotate_display_titles`` only fills missing
    ``display_title`` keys, so re-running it stays idempotent. With
    tombstones a shallow copy is filtered instead, so the raw cached
    payload keeps its full session list and a later call with a different
    tombstone set (e.g. an un-delete) still sees every session.
    """
    if deleted:
        payload = apply_deleted_filter(dict(payload), set(deleted))
    payload["sessions"] = annotate_display_titles(payload.get("sessions", []))
    return payload


@threadsafe_lru_cache(maxsize=4)
def _processed_index_cached(
    index_path: str, mtime_ns: int, size: int, deleted: frozenset[str]
) -> dict:
    """Stat- and tombstone-keyed cache of the finalized JSON payload.

    ``load_index`` runs several times per session-detail page load (#125);
    without this memo each call would redo the O(n) filter/annotate passes
    over ~1k sessions even though the raw file read is already cached.
    """
    return _finalize_index_payload(_load_index_cached(index_path, mtime_ns, size), deleted)


@threadsafe_lru_cache(maxsize=4)
def _processed_index_v2_cached(
    db_path: str,
    mtime_ns: int,
    size: int,
    deleted: frozenset[str],
    ignored_mtime_ns: int,
    ignored_size: int,
) -> dict:
    """v2-store mirror of ``_processed_index_cached``."""
    return _finalize_index_payload(
        _load_index_v2_cached(db_path, mtime_ns, size, ignored_mtime_ns, ignored_size),
        deleted,
    )


def clear_index_cache() -> None:
    """Clear the index-read caches owned by the service layer."""
    _load_index_cached.cache_clear()
    _load_index_v2_cached.cache_clear()
    _load_deleted_session_ids_cached.cache_clear()
    _processed_index_cached.cache_clear()
    _processed_index_v2_cached.cache_clear()


# --- By-id lookup over an index payload ---
#
# Memoized {id: session} map over a payload object (#125, #126). Both the
# web interface and the MCP server look sessions up by id several times per
# request; building the dict once per payload object turns each lookup from
# an O(n) scan over ~1k sessions into a dict hit. The memo holds a strong
# reference to the payload it was built from, so the identity comparison
# stays valid (no id() reuse after GC), and any change — an index rebuild or
# tombstone update (new stat/deleted key → a different payload object from
# the processed-payload caches above) or a swapped-in test fake — naturally
# produces a different object and rebuilds the map on the next call.
_SESSION_BY_ID_MEMO: Optional[tuple[dict, dict[str, dict]]] = None


def session_by_id_map(payload: dict) -> dict[str, dict]:
    """Return the memoized ``{id: session}`` mapping for ``payload``.

    First occurrence wins for duplicate ids, matching the linear
    ``next()``-based scans this replaces.
    """
    global _SESSION_BY_ID_MEMO
    memo = _SESSION_BY_ID_MEMO
    if memo is None or memo[0] is not payload:
        by_id: dict[str, dict] = {}
        for session in payload.get("sessions", []):
            session_key = session.get("id")
            if session_key and session_key not in by_id:
                by_id[session_key] = session
        memo = (payload, by_id)
        _SESSION_BY_ID_MEMO = memo
    return memo[1]


# --- v2 store ---


def _v2_enabled() -> bool:
    """Whether to read sessions from the v2 SQLite store (issue #44).

    Default is **on**. Set ``LORE_USE_V2=0`` (or false/no/off) to
    force the legacy index.json reader as an escape hatch.
    """
    raw = os.environ.get("LORE_USE_V2", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _hybrid_enabled() -> bool:
    """Whether to fuse semantic (vector) hits into keyword search (#87).

    Default is **on**. Set ``LORE_HYBRID_SEARCH=0`` (or false/no/off) to force
    keyword-only search — an escape hatch if the vector pass ever misbehaves.
    The vector backend still has to be installed for hybrid to do anything.
    """
    raw = os.environ.get("LORE_HYBRID_SEARCH", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _hybrid_search(
    index_dir: Path,
    query: str,
    tool: Optional[str],
    project: Optional[str],
    limit: int,
    *,
    fts_results: list[dict],
    include_automated: bool = False,
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict]:
    """Fuse the already-computed FTS hits with semantic (vector) hits via RRF.

    Returns the FTS results unchanged when semantic search yields nothing
    (vector backend absent, or query empty) — so hybrid never *loses* a keyword
    match, it only adds meaning-based ones.
    """
    from lore.storage import rrf_merge, semantic_search_sessions

    vec_results = semantic_search_sessions(
        index_dir,
        query,
        tool=tool,
        project=project,
        tag=tag,
        start=start,
        end=end,
        limit=limit,
        include_automated=include_automated,
    )
    if not vec_results:
        return fts_results
    return rrf_merge(fts_results, vec_results, limit=limit)


def _load_index_from_v2(index_path: Path, deleted: set[str]) -> Optional[dict]:
    """Load and post-process the index from the v2 store, or None to fall back.

    Returns the same dict shape as the JSON path (deleted-filtered, display
    titles annotated) or ``None`` when v2 is unavailable/unreadable.
    """
    if not _v2_enabled():
        return None
    try:
        from lore.storage import v2_is_available

        index_dir = index_path.parent
        # Staleness check: if a JSON index exists and is newer than the v2
        # store, the v2 store is stale — fall back to JSON (#36).
        if not v2_is_available(index_dir, compare_to=index_path):
            return None
        v2_db = index_dir / "index_v2.sqlite"
        stat = v2_db.stat()
        from lore.storage.reader import ignored_json_stat

        ignored_mtime_ns, ignored_size = ignored_json_stat(index_dir)
        payload = _processed_index_v2_cached(
            str(v2_db),
            stat.st_mtime_ns,
            stat.st_size,
            frozenset(deleted),
            ignored_mtime_ns,
            ignored_size,
        )
    except Exception as exc:
        logger.warning("v2 index read failed, falling back to JSON: %s", exc)
        return None
    return payload


# --- Index Loading ---


def load_index(
    index_path: Path,
    output_dir: Path,
    deleted: set[str],
) -> dict:
    """Load the search index, preferring the v2 store, building it if missing.

    ``deleted`` is the current tombstone set (callers pass
    ``load_deleted_session_ids(...)``).
    """
    v2_payload = _load_index_from_v2(index_path, deleted)
    if v2_payload is not None:
        return v2_payload

    if not index_path.exists():
        try:
            build_search_index(output_dir, index_path, deleted_ids=deleted)
        except Exception as exc:
            logger.warning("Failed to build index from extractors: %s", exc)
            return {"stats": {}, "sessions": []}
        # A rebuild also produces the v2 store — retry the v2 path once.
        v2_payload = _load_index_from_v2(index_path, deleted)
        if v2_payload is not None:
            return v2_payload
    if not index_path.exists():
        return {"stats": {}, "sessions": []}
    stat = index_path.stat()
    return _processed_index_cached(
        str(index_path), stat.st_mtime_ns, stat.st_size, frozenset(deleted)
    )


def load_index_summary(
    index_path: Path, deleted: set[str], include_automated: bool = False
) -> dict:
    """Return lightweight index metadata without shipping full session records.

    Automated sessions are not counted unless ``include_automated``; their number is
    reported as ``automated_hidden``.
    """
    v2_payload = _load_index_from_v2(index_path, deleted)
    if v2_payload is not None:
        visible = filter_automated_payload(v2_payload, include_automated)
        stats = visible.get("stats", {})
        v2_db = index_path.parent / "index_v2.sqlite"
        return {
            "total_sessions": int(stats.get("total_sessions") or len(visible.get("sessions", []))),
            "by_tool": dict(stats.get("by_tool") or {}),
            "last_updated": v2_payload.get("generated_at"),
            "index_size_bytes": v2_db.stat().st_size if v2_db.exists() else 0,
            "automated_hidden": int(visible.get("automated_hidden") or 0),
        }

    if not index_path.exists():
        return {
            "total_sessions": 0,
            "by_tool": {},
            "last_updated": None,
            "index_size_bytes": 0,
            "automated_hidden": 0,
        }

    stat = index_path.stat()
    payload = _load_index_cached(str(index_path), stat.st_mtime_ns, stat.st_size)

    sessions = payload.get("sessions", [])
    if deleted:
        sessions = [s for s in sessions if s.get("id") not in deleted]
    visible = filter_automated(sessions, include_automated)
    hidden = len(sessions) - len(visible)

    if deleted or hidden:
        by_tool: dict[str, int] = {}
        for session in visible:
            tool = str(session.get("tool") or "")
            if tool:
                by_tool[tool] = by_tool.get(tool, 0) + 1
        total_sessions = len(visible)
    else:
        stats = payload.get("stats", {})
        by_tool = dict(stats.get("by_tool") or {})
        total_sessions = int(stats.get("total_sessions") or 0)

    return {
        "total_sessions": total_sessions,
        "by_tool": by_tool,
        "last_updated": payload.get("generated_at"),
        "index_size_bytes": stat.st_size,
        "automated_hidden": hidden,
    }


# --- Search ---


def search_index(
    index_path: Path,
    query: str,
    deleted: set[str],
    tool: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
    scope: Optional[str] = None,
    include_automated: bool = False,
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict]:
    """Search sessions, reading from whichever store ``load_index`` uses (#34).

    When the v2 store is the active source, search v2's FTS5 index so search
    results and the session list stay consistent. Otherwise fall back to the
    legacy SearchEngine over index.sqlite.

    ``scope`` optionally restricts matches to a message-role subset (#24):
    ``"user_only"``, ``"assistant_only"`` or ``"tool_results"``. Scoped
    search needs the v2 store's per-message rows — on the legacy fallback
    path scope is a no-op.

    Sessions whose origin is exec/cron/subagent are left out unless
    ``include_automated`` is set; this holds for the keyword, hybrid and legacy paths.

    Returns ``[{"session": <dict>, "score": <float>}]``.

    ``tag``, ``start`` and ``end`` constrain the candidate set before the limit
    is applied (#174), so a filtered search still fills its limit from matching
    sessions. On the legacy fallback path they are applied after ranking,
    because the legacy store has neither saved tags nor an indexed date column.

    Raises:
        ValueError: when ``scope`` is not a recognised value, or when a date
            filter cannot be parsed.
    """
    index_dir = index_path.parent
    if _v2_enabled():
        try:
            from lore.storage import search_sessions, v2_is_available

            # Pass compare_to so a v2 store that is older than the JSON index
            # (e.g. a prune ran but the v2 dual-write failed with a constraint
            # error) is treated as stale and skipped — otherwise the legacy
            # JSON has 793 sessions and v2 has 0 and search silently returns
            # nothing.
            if v2_is_available(index_dir, compare_to=index_path):
                results = search_sessions(
                    index_dir,
                    query,
                    tool=tool,
                    project=project,
                    tag=tag,
                    start=start,
                    end=end,
                    limit=limit,
                    scope=scope,
                    include_automated=include_automated,
                )
                # Hybrid: fuse keyword hits with semantic (vector) hits so a
                # query that only matches by meaning still surfaces. Only when
                # enabled, the vector backend is present, and no role scope is
                # set (scoping is keyword-only — vectors are per-session).
                if _hybrid_enabled() and not (scope and scope != "all"):
                    results = _hybrid_search(
                        index_dir,
                        query,
                        tool,
                        project,
                        limit,
                        fts_results=results,
                        include_automated=include_automated,
                        tag=tag,
                        start=start,
                        end=end,
                    )
                if results:
                    results = filter_automated_results(results, include_automated)
                    return apply_deleted_filter_to_results(results, deleted)
                # v2 returned nothing — fall through to legacy. Better to
                # double-check than to silently lie. Legacy may still have it.
        except ValueError:
            # Invalid scope — surface to the caller, do not fall back.
            raise
        except Exception as exc:
            logger.warning("v2 search failed, falling back to legacy: %s", exc)

    # Legacy SearchEngine has no per-message rows; validate scope but treat
    # any non-"all" value as a no-op (return the unfiltered legacy result).
    from lore.storage.search import SEARCH_SCOPES

    if (scope or "all").strip().lower() not in SEARCH_SCOPES:
        raise ValueError(
            f"Invalid scope '{scope}'. Expected one of: {', '.join(sorted(SEARCH_SCOPES))}."
        )

    from lore.search.engine import SearchEngine

    # Validate the filters even on this path, so an invalid date is an error
    # rather than a silently unfiltered result set (#174).
    start_dt, end_dt = _validated_range(index_dir, start, end)

    engine = SearchEngine(index_path)
    # The engine drops automated sessions before its row cap, so a flood of them cannot
    # push the interactive hits out of the result.
    results = engine.search(query, tool=tool, project=project, include_automated=include_automated)
    results = apply_deleted_filter_to_results(results, deleted)
    if tag or start_dt or end_dt:
        results = _filter_ranked_results(results, index_dir, tag, start_dt, end_dt)
    return results[:limit]


def _validated_range(index_dir: Path, start: Optional[str], end: Optional[str]) -> tuple:
    """Parse the date filters; raises ValueError on an unparsable value (#174)."""
    from lore.storage.search import session_filter_sql

    # Reuses the v2 helper purely for its validation, so both paths reject the
    # same inputs with the same message.
    session_filter_sql(start=start, end=end, prefix="sessions")
    from lore.utils.datetime import parse_date_bound

    return (
        parse_date_bound(start) if start else None,
        parse_date_bound(end, end=True) if end else None,
    )


def _filter_ranked_results(
    results: list[dict], index_dir: Path, tag, start_dt, end_dt
) -> list[dict]:
    """Post-filter for the legacy store, which has no tags and no indexed date."""
    from lore.storage import all_tags_by_session
    from lore.utils.datetime import datetime_in_range, to_local_naive

    saved = all_tags_by_session(index_dir) if tag else {}
    wanted = str(tag).strip().lower() if tag else None
    kept: list[dict] = []
    for result in results:
        session = result.get("session", {})
        if wanted is not None:
            tags = saved.get(str(session.get("id")), [])
            if wanted not in [str(value).lower() for value in tags]:
                continue
        if start_dt or end_dt:
            raw = session.get("created")
            try:
                created = to_local_naive(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            except (TypeError, ValueError):
                continue
            if not datetime_in_range(created, start_dt, end_dt):
                continue
        kept.append(result)
    return kept
