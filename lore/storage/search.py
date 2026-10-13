"""Search over the v2 SQLite store (issue #34).

Before this, `load_index()` read the v2 store but the `/search` routes and
the MCP `search_history` tool read a *separate* legacy `index.sqlite` FTS
database — the two could disagree on which sessions exist and on titles.

`search_sessions` queries v2's unified `search_index` FTS5 table, which is
populated by `storage.writer` from the same data `load_index_v2` serves —
so search and the session list are now consistent by construction.

Returns the ``[{"session": <dict>, "score": <float>}]`` shape the legacy
``SearchEngine.search`` produced, plus an optional ``snippet`` key.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fts import (
    SEARCH_INDEX_BODY_COLUMN,
    SNIPPET_CLOSE,
    SNIPPET_OPEN,
    build_fts_query,
    phrase_of,
)
from .reader import _row_to_session_dict
from .schema import open_connection
from .writer import v2_db_path

# Valid values for the optional ``scope`` argument of ``search_sessions``.
# Maps the public scope name to the set of message ``role`` values that a
# matching message must have. ``"all"`` (or None) skips role filtering.
SEARCH_SCOPES: Dict[str, tuple] = {
    "all": (),
    "user_only": ("user",),
    "assistant_only": ("assistant",),
    "tool_results": ("tool",),
}


def search_sessions(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
    scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Full-text search sessions in the v2 store.

    Args:
        output_dir: directory holding ``index_v2.sqlite``.
        query: free-text query; terms are prefix-matched.
        tool: optional exact tool filter.
        project: optional substring project filter.
        limit: max results.
        scope: optional message-role scope. One of ``SEARCH_SCOPES`` keys.
            ``None`` or ``"all"`` searches whole sessions (default). Any other
            value keeps only sessions that have at least one message of the
            requested role whose content matches every query term.

    Returns:
        ``[{"session": <legacy session dict>, "score": <float>, "snippet":
        <str | None>}]`` ordered best-match-first. ``snippet`` is the matching
        passage of the message text in sentinel form (see ``fts.SNIPPET_OPEN``);
        convert it with ``fts.snippet_to_text`` before showing it. Empty list when the v2 store is absent, the query
        is too short, or the FTS expression is malformed.

    Raises:
        ValueError: when ``scope`` is not a recognised value.
    """
    scope_value = (scope or "all").strip().lower()
    if scope_value not in SEARCH_SCOPES:
        raise ValueError(
            f"Invalid scope '{scope}'. Expected one of: {', '.join(sorted(SEARCH_SCOPES))}."
        )
    scope_roles = SEARCH_SCOPES[scope_value]
    db = v2_db_path(output_dir)
    if not db.exists():
        return []

    fts_query = build_fts_query(query)
    # The scoped LIKE helper needs bare terms: a quoted phrase is ONE term (so the
    # words must be adjacent inside one message), anything else is split on spaces.
    phrase = phrase_of(query)
    if phrase is not None:
        terms = [" ".join(phrase.split())]
    else:
        terms = [t for t in (query or "").replace('"', " ").split() if t]
    if fts_query is None or not terms:
        return []

    conn = open_connection(db)
    try:
        rows = conn.execute(
            f"""
            SELECT s.id, s.tool, s.project, s.thread_id, s.title,
                   s.created, s.updated, s.source_path, s.source_mtime_ns,
                   s.git_branch, s.git_commit, s.metadata_json,
                   s.messages_count, s.prompt_count, s.prompt_outline,
                   s.export_path, s.total_tokens,
                   bm25(search_index) AS score,
                   snippet(search_index, {SEARCH_INDEX_BODY_COLUMN}, ?, ?, '…', 24) AS snippet
            FROM search_index
            JOIN sessions s ON s.id = search_index.entity_id
            WHERE search_index MATCH ?
              AND search_index.entity_type = 'session'
            ORDER BY score ASC
            LIMIT ?
            """,
            (SNIPPET_OPEN, SNIPPET_CLOSE, fts_query, max(1, limit) * 4),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    # For a scoped search, pre-compute which candidate sessions have at least
    # one message of the requested role whose content matches every query
    # term. Done with one query over the per-message ``messages`` table.
    scoped_session_ids: Optional[set] = None
    if scope_roles:
        candidate_ids = [row["id"] for row in rows]
        scoped_session_ids = _sessions_matching_scope(db, candidate_ids, terms, scope_roles)

    results: List[Dict[str, Any]] = []
    for row in rows:
        session = _row_to_session_dict(row)
        if tool and session.get("tool") != tool:
            continue
        if project and project not in str(session.get("project") or ""):
            continue
        if scoped_session_ids is not None and session.get("id") not in scoped_session_ids:
            continue
        results.append(
            {"session": session, "score": float(row["score"]), "snippet": row["snippet"] or None}
        )
        if len(results) >= limit:
            break
    return results


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so ``%``/``_`` in a term match literally (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sessions_matching_scope(
    db: Path,
    session_ids: List[str],
    terms: List[str],
    roles: tuple,
) -> set:
    """Return the subset of ``session_ids`` with a matching message in ``roles``.

    A message matches when its role is in ``roles`` and its content contains
    every term in ``terms`` (case-insensitive substring match, same prefix
    semantics as the FTS query). Used to scope a search to user/assistant/
    tool-result messages only.
    """
    if not session_ids:
        return set()

    id_placeholders = ",".join("?" for _ in session_ids)
    role_placeholders = ",".join("?" for _ in roles)
    term_clauses = " AND ".join("LOWER(content) LIKE ? ESCAPE '\\'" for _ in terms)
    like_params = [f"%{_escape_like(t.lower())}%" for t in terms]

    sql = (
        f"SELECT DISTINCT session_id FROM messages "
        f"WHERE session_id IN ({id_placeholders}) "
        f"AND role IN ({role_placeholders}) "
        f"AND {term_clauses}"
    )

    conn = open_connection(db)
    try:
        rows = conn.execute(sql, [*session_ids, *roles, *like_params]).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {row["session_id"] for row in rows}


def semantic_search_sessions(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Vector (KNN) search over the session archive.

    Embeds ``query`` and finds the nearest ``session_embeddings`` vectors via
    sqlite-vec, joining back to ``sessions`` for the display dict. Returns the
    same ``[{"session": <dict>, "score": <float>}]`` shape as
    :func:`search_sessions` (score = vec distance, smaller = closer), so it
    fuses cleanly with the FTS results.

    Returns an empty list — never raises — when the vector table or the
    embedding backend is unavailable, the query is empty, or it can't be
    embedded. Callers then rely on FTS alone. ``scope`` is intentionally not
    supported: vectors are per-session, so role scoping has no vector analogue.
    """
    from .embeddings import embed_text, pack_vector, sqlite_vec_available

    if not sqlite_vec_available():
        return []
    if not (query or "").strip():
        return []
    db = v2_db_path(output_dir)
    if not db.exists():
        return []

    vector = embed_text(query)
    if vector is None:
        return []

    conn = open_connection(db)
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.tool, s.project, s.thread_id, s.title,
                   s.created, s.updated, s.source_path, s.source_mtime_ns,
                   s.git_branch, s.git_commit, s.metadata_json,
                   s.messages_count, s.prompt_count, s.prompt_outline,
                   s.export_path, s.total_tokens,
                   se.distance AS score
            FROM session_embeddings se
            JOIN sessions s ON s.id = se.session_id
            WHERE se.embedding MATCH ?
              AND k = ?
            ORDER BY se.distance ASC
            """,
            (pack_vector(vector), max(1, limit) * 4),
        ).fetchall()
    except sqlite3.Error:
        # No vec table (e.g. extension absent) or a malformed query — FTS covers it.
        return []
    finally:
        conn.close()

    results: List[Dict[str, Any]] = []
    for row in rows:
        session = _row_to_session_dict(row)
        if tool and session.get("tool") != tool:
            continue
        if project and project not in str(session.get("project") or ""):
            continue
        results.append({"session": session, "score": float(row["score"])})
        if len(results) >= limit:
            break
    return results
