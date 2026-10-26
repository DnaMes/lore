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

import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from lore.core.models import AUTOMATED_ORIGINS

from .fts import (
    SEARCH_INDEX_BODY_COLUMN,
    SNIPPET_CLOSE,
    SNIPPET_OPEN,
    build_fts_query,
)
from .reader import (
    _row_to_session_dict,
    automated_exclusion_sql,
    load_ignored_session_ids,
    session_contract_select,
)
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

# Tokenizer of the throwaway scope index. It must be identical to the one on
# ``search_index`` (migration 4) so the role check and the query that produced
# the candidates tokenize text the same way — pinned by
# ``test_scope_tokenizer_matches_the_session_index``.
SCOPE_FTS_TOKENIZER = "porter unicode61"


def search_sessions(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
    scope: Optional[str] = None,
    include_automated: bool = False,
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
        include_automated: also return sessions whose origin is exec/cron/subagent
            (hidden by default).

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
    if fts_query is None:
        return []

    ignored = load_ignored_session_ids(output_dir)
    conn = open_connection(db)
    try:
        exclusion = "" if include_automated else automated_exclusion_sql(conn, "s")
        rows = conn.execute(
            f"""
            SELECT s.id, s.tool, s.project, s.thread_id, s.title,
                   s.created, s.updated, s.source_path, s.source_mtime_ns,
                   s.git_branch, s.git_commit, s.metadata_json,
                   s.messages_count, s.prompt_count, s.prompt_outline,
                   s.export_path, s.total_tokens,
                   {session_contract_select(conn, "s")},
                   bm25(search_index) AS score,
                   snippet(search_index, {SEARCH_INDEX_BODY_COLUMN}, ?, ?, '…', 24) AS snippet
            FROM search_index
            JOIN sessions s ON s.id = search_index.entity_id
            WHERE search_index MATCH ?
              AND search_index.entity_type = 'session'{exclusion}
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
        scoped_session_ids = _sessions_matching_scope(db, candidate_ids, fts_query, scope_roles)

    results: List[Dict[str, Any]] = []
    for row in rows:
        session = _row_to_session_dict(row)
        if str(session.get("id") or "") in ignored:
            continue
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


def _sessions_matching_scope(
    db: Path,
    session_ids: List[str],
    fts_query: str,
    roles: tuple,
) -> set:
    """Return the subset of ``session_ids`` with a message in ``roles`` matching ``fts_query``.

    The role check must agree with the FTS query that produced the candidates,
    otherwise a scoped search returns hits that same query would not accept
    (#199). The old implementation was a ``LOWER(content) LIKE '%term%'``
    substring test, which both over- and under-matched the index: ``cat``
    accepted a message containing only "concatenate", and ``cafe`` rejected
    "café", which ``search_index`` matches after folding diacritics.

    SQLite only indexes messages inside the session body, so each candidate
    session's messages of the requested roles are copied into a throwaway
    ``:memory:`` FTS5 table built with the *same* tokenizer and queried with the
    *same* expression (``build_fts_query``) that matched the session itself.
    Nothing is written to the archive, and sessions are processed one at a time
    so the transient index never exceeds one session's messages (measured
    0.20-0.34 s for 20 000 candidate messages, 12 MB, versus 0.04 s for LIKE).

    Returns an empty set when nothing matches or the throwaway index cannot be
    used — the same "no scoped hits" outcome the old code had on ``sqlite3.Error``.
    """
    if not session_ids or not fts_query:
        return set()

    role_placeholders = ",".join("?" for _ in roles)
    insert_sql = (
        "INSERT INTO scope_check.matches (session_id, body) "
        "SELECT m.session_id, m.content FROM messages m "
        f"WHERE m.session_id = ? AND m.role IN ({role_placeholders})"
    )
    matched: set = set()
    conn = open_connection(db)
    try:
        conn.execute("ATTACH DATABASE ':memory:' AS scope_check")
        conn.execute(
            "CREATE VIRTUAL TABLE scope_check.matches USING fts5("
            f"session_id UNINDEXED, body, tokenize='{SCOPE_FTS_TOKENIZER}')"
        )
        for session_id in session_ids:
            if session_id in matched:
                continue  # one matching message is enough for this session
            conn.execute(insert_sql, (session_id, *roles))
            hit = conn.execute(
                "SELECT 1 FROM scope_check.matches WHERE matches MATCH ? LIMIT 1",
                (fts_query,),
            ).fetchone()
            if hit:
                matched.add(session_id)
            conn.execute("DELETE FROM scope_check.matches")
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return matched


# KNN width: ``k = limit * KNN_K_FACTOR`` neighbours are fetched before the tool, project
# and origin filters run. When that leaves fewer than ``limit`` hits the query is repeated
# once with ``k * KNN_K_RETRY_FACTOR``, never above ``KNN_K_MAX`` (sqlite-vec accepts
# k up to 4096; 1024 keeps the second pass cheap on large archives).
KNN_K_FACTOR = 4
KNN_K_RETRY_FACTOR = 8
KNN_K_MAX = 1024

# Distance cutoff for vector neighbours, applied before RRF fusion (#92). sqlite-vec's
# default metric on ``session_embeddings`` is L2 over unit-normalised bge-small vectors
# (``distance = sqrt(2 - 2*cosine)``), so smaller is closer and 0 is identical.
#
# 0.88 is measured, not invented: on the labelled set in
# docs/research/2026-10-14-semantic-cutoff-labelled-pairs.json the related distances run
# 0.610-0.794 (p95 0.777) and the unrelated ones 0.980-1.104 (p05 0.986), so the cutoff
# sits at the midpoint of related-p95 / unrelated-p05 (0.881 -> 0.88) and on that set
# keeps 14/14 related while dropping 14/14 unrelated. Reproduce with
# ``python tools/calibrate_semantic_cutoff.py``.
SEMANTIC_MAX_DISTANCE_ENV = "LORE_SEMANTIC_MAX_DISTANCE"
DEFAULT_SEMANTIC_MAX_DISTANCE = 0.88


def semantic_max_distance() -> Optional[float]:
    """Vec distance above which a neighbour is dropped; ``None`` means no cutoff.

    Env ``LORE_SEMANTIC_MAX_DISTANCE`` overrides the calibrated default; a value
    of ``0`` (or negative) disables the cutoff entirely, an unparsable or NaN
    value falls back to the default. Never raised on: a bad value must not
    break search (#92).
    """
    raw = os.environ.get(SEMANTIC_MAX_DISTANCE_ENV, "").strip()
    if not raw:
        return DEFAULT_SEMANTIC_MAX_DISTANCE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SEMANTIC_MAX_DISTANCE
    if math.isnan(value):
        return DEFAULT_SEMANTIC_MAX_DISTANCE
    return None if value <= 0 else value


def _knn_rows(conn: sqlite3.Connection, packed: bytes, k: int) -> List[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT s.id, s.tool, s.project, s.thread_id, s.title,
               s.created, s.updated, s.source_path, s.source_mtime_ns,
               s.git_branch, s.git_commit, s.metadata_json,
               s.messages_count, s.prompt_count, s.prompt_outline,
               s.export_path, s.total_tokens,
               {session_contract_select(conn, "s")},
               se.distance AS score
        FROM session_embeddings se
        JOIN sessions s ON s.id = se.session_id
        WHERE se.embedding MATCH ?
          AND k = ?
        ORDER BY se.distance ASC
        """,
        (packed, k),
    ).fetchall()


def _filter_semantic_rows(
    rows: List[sqlite3.Row],
    tool: Optional[str],
    project: Optional[str],
    limit: int,
    include_automated: bool,
    *,
    max_distance: Optional[float],
    ignored: Optional[set] = None,
) -> List[Dict[str, Any]]:
    ignored_ids = ignored or set()
    results: List[Dict[str, Any]] = []
    for row in rows:
        if max_distance is not None and float(row["score"]) > max_distance:
            continue  # too far away to mean anything (#92) — dropped before fusion
        session = _row_to_session_dict(row)
        if str(session.get("id") or "") in ignored_ids:
            continue
        if not include_automated and session.get("origin") in AUTOMATED_ORIGINS:
            continue
        if tool and session.get("tool") != tool:
            continue
        if project and project not in str(session.get("project") or ""):
            continue
        results.append({"session": session, "score": float(row["score"])})
        if len(results) >= limit:
            break
    return results


def semantic_search_sessions(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
    include_automated: bool = False,
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

    Neighbours farther away than :func:`semantic_max_distance` are dropped
    here, *before* RRF fusion (#92), so padding sessions never enter the fused
    ranking. Set ``LORE_SEMANTIC_MAX_DISTANCE=0`` to turn the cutoff off.
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

    ignored = load_ignored_session_ids(output_dir)
    conn = open_connection(db)
    try:
        packed = pack_vector(vector)
        cutoff = semantic_max_distance()
        k = max(1, limit) * KNN_K_FACTOR
        rows = _knn_rows(conn, packed, k)
        results = _filter_semantic_rows(
            rows,
            tool,
            project,
            limit,
            include_automated,
            max_distance=cutoff,
            ignored=ignored,
        )
        # The filters run after the KNN cut-off, so a neighbourhood made of automated
        # (or other filtered-out) sessions can leave fewer than ``limit`` hits. Ask once
        # more with a wider k, only when the first pass was full (more neighbours exist).
        if len(results) < limit and len(rows) >= k and k < KNN_K_MAX:
            wider = min(k * KNN_K_RETRY_FACTOR, KNN_K_MAX)
            rows = _knn_rows(conn, packed, wider)
            results = _filter_semantic_rows(
                rows,
                tool,
                project,
                limit,
                include_automated,
                max_distance=cutoff,
                ignored=ignored,
            )
    except sqlite3.Error:
        # No vec table (e.g. extension absent) or a malformed query — FTS covers it.
        return []
    finally:
        conn.close()
    return results
