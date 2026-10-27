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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lore.core.models import AUTOMATED_ORIGINS

from ..utils.datetime import datetime_in_range, parse_date_bound, to_local_naive
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

# Prefer a bound ``NOT IN`` for small ignore sets; above this, load ids into a
# TEMP table so we stay under SQLite's ~32766 variable limit.
_IGNORED_SQL_CHUNK = 500


def _bind_ignored_exclusion(
    conn: sqlite3.Connection,
    column_sql: str,
    ignored: set[str],
) -> tuple[str, tuple]:
    """Return ``(sql_fragment, params)`` excluding ``ignored`` before LIMIT.

    Empty set → no-op. Small sets → ``AND col NOT IN (?,?,…)``. Larger sets →
    TEMP table + ``NOT IN (SELECT …)``, inserted in chunks of
    ``_IGNORED_SQL_CHUNK``.
    """
    if not ignored:
        return "", ()
    ids = sorted(str(i) for i in ignored if i)
    if not ids:
        return "", ()
    if len(ids) <= _IGNORED_SQL_CHUNK:
        placeholders = ",".join("?" for _ in ids)
        return f" AND {column_sql} NOT IN ({placeholders})", tuple(ids)

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _lore_ignored_ids (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _lore_ignored_ids")
    for offset in range(0, len(ids), _IGNORED_SQL_CHUNK):
        chunk = ids[offset : offset + _IGNORED_SQL_CHUNK]
        conn.executemany(
            "INSERT OR IGNORE INTO _lore_ignored_ids(id) VALUES (?)",
            [(session_id,) for session_id in chunk],
        )
    return f" AND {column_sql} NOT IN (SELECT id FROM _lore_ignored_ids)", ()


def _like_escape(value: str) -> str:
    """Escape ``%`` and ``_`` so a project filter is a literal substring match."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def session_filter_sql(
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    prefix: str = "s",
) -> tuple:
    """SQL that constrains candidate rows *before* the LIMIT (#174).

    Every public search path over-fetched an unfiltered candidate list and
    dropped non-matching rows in Python afterwards, so a query whose best
    unfiltered hits lived in another project or tool returned too few — or no —
    results even though matching sessions existed.

    Returns ``(sql, params)`` where ``sql`` is a sequence of ``AND …`` clauses
    for the query's ``WHERE`` and ``params`` its bound values. Date bounds are
    compared on the ``YYYY-MM-DD`` prefix, which is the granularity every
    surface exposes and which sorts correctly for ISO timestamps; the exact
    ``[start, end]`` check still runs afterwards through
    :func:`lore.utils.datetime.datetime_in_range` so an offset-carrying stored
    value cannot slip into the wrong day.

    Raises:
        ValueError: when a date filter is not a date or timestamp.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if tool:
        clauses.append(f"AND {prefix}.tool = ?")
        params.append(str(tool))
    if project:
        clauses.append(f"AND {prefix}.project LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(str(project))}%")
    if tag:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM session_tags st "
            f"WHERE st.session_id = {prefix}.id AND st.tag = ?)"
        )
        params.append(str(tag).strip().lower())
    for value, operator, label in ((start, ">=", "start"), (end, "<=", "end")):
        if value is None or str(value).strip() == "":
            continue
        bound = parse_date_bound(value, end=label == "end")
        if bound is None:
            raise ValueError(
                f"Invalid {label} date '{value}'. Expected YYYY-MM-DD or an ISO timestamp."
            )
        clauses.append(f"AND substr({prefix}.created, 1, 10) {operator} ?")
        params.append(bound.date().isoformat())
    return "".join(f"\n              {clause}" for clause in clauses), tuple(params)


def _range_bounds(start: Optional[str], end: Optional[str]) -> tuple:
    """The exact ``(start, end)`` boundaries for the post-filter, or ``(None, None)``."""
    return (
        parse_date_bound(start) if start else None,
        parse_date_bound(end, end=True) if end else None,
    )


def search_sessions(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    scope: Optional[str] = None,
    include_automated: bool = False,
) -> List[Dict[str, Any]]:
    """Full-text search sessions in the v2 store.

    Args:
        output_dir: directory holding ``index_v2.sqlite``.
        query: free-text query; terms are prefix-matched.
        tool: optional exact tool filter.
        project: optional substring project filter.
        tag: optional saved tag (``session_tags``) the session must carry.
        start/end: optional date or ISO timestamp bounds; both are inclusive and
            a date-only ``end`` covers the whole day.
        limit: max results.
        offset: number of matching sessions to skip, for paging (#175).
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
        ValueError: when ``scope`` is not a recognised value, or when a date
            filter cannot be parsed.
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
    # #174: every filter is part of the candidate selection, so a limit of N
    # returns N matching sessions instead of N - k after post-filtering.
    filter_sql, filter_params = session_filter_sql(
        tool=tool, project=project, tag=tag, start=start, end=end
    )
    start_dt, end_dt = _range_bounds(start, end)
    # Role scope still needs the per-message table, so it keeps a margin; an
    # offset has to skip matching rows, which the scope filter decides later.
    window_offset = max(0, int(offset))
    candidate_limit = max(1, limit) * 4 if scope_roles else max(1, limit)
    conn = open_connection(db)
    try:
        exclusion = "" if include_automated else automated_exclusion_sql(conn, "s")
        ignored_sql, ignored_params = _bind_ignored_exclusion(conn, "s.id", ignored)
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
              AND search_index.entity_type = 'session'{exclusion}{ignored_sql}{filter_sql}
            ORDER BY score ASC, search_index.entity_id ASC
            LIMIT ? OFFSET ?
            """,
            (
                SNIPPET_OPEN,
                SNIPPET_CLOSE,
                fts_query,
                *ignored_params,
                *filter_params,
                candidate_limit,
                window_offset if not scope_roles else 0,
            ),
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
    skipped = 0
    for row in rows:
        session = _row_to_session_dict(row)
        if tool and session.get("tool") != tool:
            continue
        if project and project not in str(session.get("project") or ""):
            continue
        if start_dt or end_dt:
            # The SQL narrowed on the date prefix; this keeps the exact window,
            # including offset-carrying stored values.
            try:
                created_value = to_local_naive(
                    datetime.fromisoformat(str(session.get("created")).replace("Z", "+00:00"))
                )
            except (TypeError, ValueError):
                continue
            if not datetime_in_range(created_value, start_dt, end_dt):
                continue
        if scoped_session_ids is not None and session.get("id") not in scoped_session_ids:
            continue
        if scoped_session_ids is not None and skipped < window_offset:
            skipped += 1
            continue
        results.append(
            {"session": session, "score": float(row["score"]), "snippet": row["snippet"] or None}
        )
        if len(results) >= limit:
            break
    if results:
        # (#175) Name the message a hit is based on, so a result is evidence
        # rather than a claim. One connection, one throwaway index, one pass.
        evidence = message_evidence_for_sessions(
            output_dir,
            [str(entry["session"].get("id")) for entry in results],
            query,
            roles=scope_roles,
        )
        for entry in results:
            matches = evidence.get(str(entry["session"].get("id")), [])
            entry["evidence"] = matches[0] if matches else None
    return results


def count_session_hits(
    output_dir: Path,
    query: str,
    *,
    tool: Optional[str] = None,
    project: Optional[str] = None,
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_automated: bool = False,
    exclude_ids: Optional[set] = None,
) -> Optional[int]:
    """Exact number of matching sessions, for an explicit search total (#175).

    Uses the identical WHERE clause (and therefore the identical filters) as
    :func:`search_sessions`, so the total can never disagree with the pages.
    Returns ``None`` when the v2 store is absent or the query is unusable.

    A message-role ``scope`` is deliberately not applied: scoping needs the
    per-message table, so a scoped caller must mark its total as partial rather
    than compare it with a filtered page.
    """
    db = v2_db_path(output_dir)
    if not db.exists():
        return None
    fts_query = build_fts_query(query)
    if fts_query is None:
        return None
    ignored = set(load_ignored_session_ids(output_dir))
    # Soft-deleted sessions are not reachable by paging, so they must not be
    # counted either (#175).
    ignored.update(str(value) for value in (exclude_ids or ()) if value)
    filter_sql, filter_params = session_filter_sql(
        tool=tool, project=project, tag=tag, start=start, end=end
    )
    conn = open_connection(db)
    try:
        exclusion = "" if include_automated else automated_exclusion_sql(conn, "s")
        ignored_sql, ignored_params = _bind_ignored_exclusion(conn, "s.id", ignored)
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM search_index
            JOIN sessions s ON s.id = search_index.entity_id
            WHERE search_index MATCH ?
              AND search_index.entity_type = 'session'{exclusion}{ignored_sql}{filter_sql}
            """,
            (fts_query, *ignored_params, *filter_params),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0]) if row else None


def _excerpt_elided(excerpt: str, body: str) -> bool:
    """Whether the excerpt omits part of the message.

    FTS5 emits a window of ``n`` tokens around the match, so anything outside it
    is dropped — sometimes with, sometimes without an ellipsis glyph. The only
    reliable test is coverage: an excerpt is complete exactly when its plain text
    (markers and ellipses removed, whitespace normalised) equals the message.
    """
    plain = " ".join(
        excerpt.replace(SNIPPET_OPEN, "").replace(SNIPPET_CLOSE, "").replace("…", " ").split()
    )
    if not plain:
        return True
    return plain != " ".join(body.split())


def session_message_evidence(
    output_dir: Path,
    session_id: str,
    query: str,
    *,
    roles: tuple = (),
    limit: int = 1,
) -> List[Dict[str, Any]]:
    """Evidence for one session; see :func:`message_evidence_for_sessions`."""
    return message_evidence_for_sessions(
        output_dir, [session_id], query, roles=roles, per_session=limit
    ).get(str(session_id), [])


def message_evidence_for_sessions(
    output_dir: Path,
    session_ids: List[str],
    query: str,
    *,
    roles: tuple = (),
    per_session: int = 1,
) -> Dict[str, List[Dict[str, Any]]]:
    """Messages of ``session_id`` that match ``query``, with a bounded excerpt (#175).

    Session hits alone cannot show *where* a match is, which is what a search
    result needs to be evidence rather than a claim. The rule is the same one
    the scoped search uses: messages are copied into a throwaway FTS5 table with
    the session index's tokenizer and queried with the same expression, so an
    excerpt cannot claim a match the session search would not accept.

    Returns up to ``limit`` entries ordered by ``seq``::

        [{"seq": int, "role": str, "excerpt": str, "truncated": bool}]

    ``truncated`` is true when FTS5 shortened the passage — the caller must not
    present such an excerpt as the whole message.
    """
    db = v2_db_path(output_dir)
    ids = [str(session_id) for session_id in session_ids if session_id]
    if not db.exists() or not ids:
        return {}
    fts_query = build_fts_query(query)
    if fts_query is None:
        return {}

    role_sql = ""
    role_params: tuple = ()
    if roles:
        role_sql = f" AND m.role IN ({','.join('?' for _ in roles)})"
        role_params = tuple(roles)

    conn = open_connection(db)
    try:
        conn.execute("ATTACH DATABASE ':memory:' AS evidence_check")
        conn.execute(
            "CREATE VIRTUAL TABLE evidence_check.messages USING fts5("
            "session_id UNINDEXED, seq UNINDEXED, role UNINDEXED, body, "
            f"tokenize='{SCOPE_FTS_TOKENIZER}')"
        )
        found: Dict[str, List[Dict[str, Any]]] = {}
        for session_id in ids:
            conn.execute("DELETE FROM evidence_check.messages")
            conn.execute(
                "INSERT INTO evidence_check.messages (session_id, seq, role, body) "
                "SELECT m.session_id, m.seq, m.role, m.content FROM messages m "
                f"WHERE m.session_id = ?{role_sql}",
                (session_id, *role_params),
            )
            rows = conn.execute(
                "SELECT seq, role, body, "
                # The snippet() argument is a bare table name; a schema prefix
                # is parsed as a column and fails.
                "snippet(messages, 3, ?, ?, '…', 20) AS excerpt "
                "FROM evidence_check.messages WHERE messages MATCH ? "
                "ORDER BY CAST(seq AS INTEGER) LIMIT ?",
                (SNIPPET_OPEN, SNIPPET_CLOSE, fts_query, max(1, int(per_session))),
            ).fetchall()
            if not rows:
                continue
            found[session_id] = [
                {
                    "seq": int(row["seq"]),
                    "role": row["role"],
                    "excerpt": row["excerpt"] or "",
                    # An elided passage is evidence, not the whole message
                    # (#175). Decided by content, not by the ellipsis glyph:
                    # FTS5 also marks a window that starts mid-message even when
                    # nothing was removed.
                    "truncated": _excerpt_elided(row["excerpt"] or "", row["body"] or ""),
                }
                for row in rows
            ]
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return found


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
# tools/calibration/semantic-cutoff-labelled-pairs.json the related distances run
# 0.610-0.794 (p95 0.777) and the unrelated ones 0.980-1.104 (p05 0.986), so the cutoff
# sits at the midpoint of related-p95 / unrelated-p05 (0.881 -> 0.88) and on that set
# keeps 14/14 related while dropping 14/14 unrelated. Reproduce with
# ``python tools/calibration/calibrate_semantic_cutoff.py``.
SEMANTIC_MAX_DISTANCE_ENV = "LORE_SEMANTIC_MAX_DISTANCE"
DEFAULT_SEMANTIC_MAX_DISTANCE = 0.88


def _distance_cutoff(env_name: str, default: float) -> Optional[float]:
    """Parse a distance cutoff from ``env_name``; ``None`` means no cutoff.

    Shared by the session-level (:func:`semantic_max_distance`) and
    message-level (:func:`lore.storage.message_search.message_max_distance`)
    cutoffs so both parse the same way: unset → ``default``, ``0`` (or
    negative) → cutoff disabled, unparsable/NaN → ``default``. Never raises: a
    bad value must not break search (#92).
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value):
        return default
    return None if value <= 0 else value


def semantic_max_distance() -> Optional[float]:
    """Vec distance above which a session neighbour is dropped; ``None`` disables.

    Env ``LORE_SEMANTIC_MAX_DISTANCE`` overrides the calibrated default; see
    :func:`_distance_cutoff` for the parsing rules.
    """
    return _distance_cutoff(SEMANTIC_MAX_DISTANCE_ENV, DEFAULT_SEMANTIC_MAX_DISTANCE)


def _knn_rows(
    conn: sqlite3.Connection,
    packed: bytes,
    k: int,
    *,
    ignored_sql: str = "",
    ignored_params: tuple = (),
    filter_sql: str = "",
    filter_params: tuple = (),
) -> List[sqlite3.Row]:
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
          AND k = ?{ignored_sql}{filter_sql}
        ORDER BY se.distance ASC
        """,
        (packed, k, *ignored_params, *filter_params),
    ).fetchall()


def _filter_semantic_rows(
    rows: List[sqlite3.Row],
    tool: Optional[str],
    project: Optional[str],
    limit: int,
    include_automated: bool,
    *,
    max_distance: Optional[float],
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    # sqlite-vec returns equally distant neighbours in an order that depends on
    # k, so the same query could rank ties differently per page. Sort by
    # (distance, session id) for a stable ranking that paging can tile (#175).
    rows = sorted(rows, key=lambda row: (float(row["score"]), str(row["id"])))
    results: List[Dict[str, Any]] = []
    for row in rows:
        if max_distance is not None and float(row["score"]) > max_distance:
            continue  # too far away to mean anything (#92) — dropped before fusion
        session = _row_to_session_dict(row)
        if not include_automated and session.get("origin") in AUTOMATED_ORIGINS:
            continue
        if tool and session.get("tool") != tool:
            continue
        if project and project not in str(session.get("project") or ""):
            continue
        if start_dt or end_dt:
            try:
                created_value = to_local_naive(
                    datetime.fromisoformat(str(session.get("created")).replace("Z", "+00:00"))
                )
            except (TypeError, ValueError):
                continue
            if not datetime_in_range(created_value, start_dt, end_dt):
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
    tag: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
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

    # #174: constrain the neighbour search itself. sqlite-vec applies the extra
    # WHERE before it finalises the k nearest, so filtered-out neighbours no
    # longer consume the budget.
    filter_sql, filter_params = session_filter_sql(
        tool=tool, project=project, tag=tag, start=start, end=end, prefix="s"
    )
    start_dt, end_dt = _range_bounds(start, end)
    ignored = load_ignored_session_ids(output_dir)
    conn = open_connection(db)
    try:
        packed = pack_vector(vector)
        cutoff = semantic_max_distance()
        ignored_sql, ignored_params = _bind_ignored_exclusion(conn, "se.session_id", ignored)
        # sqlite-vec applies extra WHERE after the k nearest are chosen, so
        # inflate k by the ignore-set size or pruned neighbours crowd out keeps.
        k = min(KNN_K_MAX, max(1, limit) * KNN_K_FACTOR + len(ignored))
        rows = _knn_rows(
            conn,
            packed,
            k,
            ignored_sql=ignored_sql,
            ignored_params=ignored_params,
            filter_sql=filter_sql,
            filter_params=filter_params,
        )
        results = _filter_semantic_rows(
            rows,
            tool,
            project,
            limit,
            include_automated,
            max_distance=cutoff,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        # The filters run after the KNN cut-off, so a neighbourhood made of automated
        # (or other filtered-out) sessions can leave fewer than ``limit`` hits. Ask once
        # more with a wider k, only when the first pass was full (more neighbours exist).
        if len(results) < limit and len(rows) >= k and k < KNN_K_MAX:
            wider = min(k * KNN_K_RETRY_FACTOR + len(ignored), KNN_K_MAX)
            rows = _knn_rows(
                conn,
                packed,
                wider,
                ignored_sql=ignored_sql,
                ignored_params=ignored_params,
                filter_sql=filter_sql,
                filter_params=filter_params,
            )
            results = _filter_semantic_rows(
                rows,
                tool,
                project,
                limit,
                include_automated,
                max_distance=cutoff,
                start_dt=start_dt,
                end_dt=end_dt,
            )
    except sqlite3.Error:
        # No vec table (e.g. extension absent) or a malformed query — FTS covers it.
        return []
    finally:
        conn.close()
    return results
