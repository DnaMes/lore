"""Message-level retrieval over the v2 store (#190).

Session search answers "which session", but a 200-message transcript still
hides *where* the answer is. This module returns the messages themselves, each
with a stable identity (:mod:`lore.storage.evidence`) and the
``/session/<id>/m/<seq>`` permalink the web UI serves, so a hit is evidence
rather than a claim.

Two paths, and the result says which one ran:

``vector``
    KNN over the per-message ``message_embeddings`` vec0 table (populated by
    :mod:`lore.storage.message_vectors`). Finds a passage by meaning — also
    late in a session — and drops neighbours farther than
    :func:`message_max_distance`, so an unrelated query returns nothing instead
    of its nearest, still-meaningless neighbour (#92).

``fts``
    Keyword search over message *bodies*. The session FTS index (whose body is
    the concatenated message text) picks a bounded set of candidate sessions,
    then their messages are copied into a throwaway in-memory FTS5 table with
    the same tokenizer and the same expression, and ranked there. This is the
    honest fallback when the optional ``lore[semantic]`` extra is missing: it
    can miss a paraphrase, but it never invents a hit.

The vector path runs when the backend is installed *and* the table holds
vectors; the FTS path runs when it does not, or when the vector neighbourhood
is empty. ``search_messages`` therefore never returns more than ``limit``
hits, and never a hit whose message row no longer exists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from lore.core.models import AUTOMATED_ORIGINS

from .embeddings import embed_text, embeddings_available, pack_vector, sqlite_vec_available
from .evidence import identity_for_message
from .fts import SNIPPET_CLOSE, SNIPPET_OPEN, build_fts_query, snippet_to_text
from .message_vectors import _content_hash
from .reader import automated_exclusion_sql, load_ignored_session_ids
from .schema import open_connection
from .search import (
    KNN_K_FACTOR,
    KNN_K_MAX,
    SCOPE_FTS_TOKENIZER,
    _bind_ignored_exclusion,
    _distance_cutoff,
    _excerpt_elided,
)
from .writer import v2_db_path

#: Hard cap on returned hits, whatever the caller asks for: a single message
#: list in a terminal or API response stays readable and the KNN window bounded.
MAX_LIMIT = 100

#: Characters of a message kept as its excerpt on the vector path. That path has
#: no query-anchored snippet (the match is by meaning, not by term), so the head
#: of the message is shown and ``excerpt_truncated`` says the rest was cut.
EXCERPT_CHARS = 400

#: FTS fallback: how many session hits seed the message index, and the multiplier
#: applied to ``limit`` so a query with many hits still has candidates to rank.
FTS_CANDIDATE_SESSIONS = 50
FTS_CANDIDATE_FACTOR = 5

# Distance cutoff for message neighbours (#190). sqlite-vec's default metric on
# ``message_embeddings`` is L2 over unit-normalised bge-small vectors
# (``distance = sqrt(2 - 2*cosine)``), so smaller is closer and 0 is identical.
#
# 0.72 is measured, not invented: on the labelled evaluation set in
# tools/calibration/message-retrieval-eval.json (2026-11-03, bge-small-en-v1.5)
# the four hand-labelled query/passage pairs run 0.556-0.617 (p95 0.617) and the
# passages of every *other* session run 0.747-1.145 (p05 0.817). The cutoff sits
# at the midpoint of related-p95 / unrelated-p05 (0.717 -> 0.72), keeps all four
# labelled passages and drops every cross-topic one; the same set's negative
# queries start at 0.844, so they return no neighbour at all. Reproduce with
# ``python tools/calibration/calibrate_message_cutoff.py``.
MESSAGE_SEMANTIC_MAX_DISTANCE_ENV = "LORE_MESSAGE_SEMANTIC_MAX_DISTANCE"
DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE = 0.72


def message_max_distance() -> Optional[float]:
    """Vec distance above which a message neighbour is dropped; ``None`` disables.

    Env ``LORE_MESSAGE_SEMANTIC_MAX_DISTANCE`` overrides the calibrated
    default; see :func:`lore.storage.search._distance_cutoff` for the parsing
    rules (``0`` disables, an unparsable value falls back to the default).
    """
    return _distance_cutoff(
        MESSAGE_SEMANTIC_MAX_DISTANCE_ENV, DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE
    )


@dataclass(frozen=True)
class MessageSearchResult:
    """One message-level search: which path produced the hits, and the hits."""

    path: str  # "vector" | "fts"
    hits: List[Dict[str, Any]]


def _hit(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    seq: int,
    role: Optional[str],
    score: float,
    excerpt: str,
    truncated: bool,
) -> Dict[str, Any]:
    """One result row: the message, its stable evidence identity and its link."""
    return {
        "session_id": session_id,
        "seq": int(seq),
        "role": role,
        "score": float(score),
        "excerpt": excerpt,
        "excerpt_truncated": bool(truncated),
        "evidence": identity_for_message(conn, session_id, int(seq)),
        # Same route as ``lore.interfaces.api_payloads.message_permalink``; the
        # storage layer cannot import the interfaces package.
        "permalink": f"/session/{session_id}/m/{int(seq)}",
        "source": "message",
    }


def _search_messages_vector(
    db: Path,
    query: str,
    limit: int,
    ignored: set,
) -> Optional[List[Dict[str, Any]]]:
    """KNN over ``message_embeddings``; ``None`` when the vector path cannot run.

    ``None`` (not ``[]``) is the "no vector path here" signal: the caller then
    runs the FTS path. Any sqlite or embedding failure degrades the same way,
    so a broken optional backend is never fatal.

    A hit whose stored ``content_hash`` differs from the hash of the current
    body is skipped: between the embedding pass and the writer replacing the
    text, the vector still describes the old content and must not surface it.
    """
    if not sqlite_vec_available() or not embeddings_available():
        return None
    conn = open_connection(db)
    try:
        if conn.execute("SELECT 1 FROM message_embeddings LIMIT 1").fetchone() is None:
            return None  # table never populated: do not spend an embed call
        vector = embed_text(query)
        if vector is None:
            return None
        cutoff = message_max_distance()
        # Extra WHERE clauses run after the k nearest are chosen, so inflate k
        # by the ignore set or pruned neighbours crowd out the keeps.
        k = min(KNN_K_MAX, max(1, limit) * KNN_K_FACTOR + len(ignored))
        rows = conn.execute(
            """
            SELECT meta.session_id, meta.seq, s.origin, m.role, m.content,
                   meta.content_hash, mem.distance AS score
            FROM message_embeddings mem
            JOIN message_embedding_meta meta ON meta.message_key = mem.message_key
            JOIN messages m ON m.session_id = meta.session_id AND m.seq = meta.seq
            JOIN sessions s ON s.id = meta.session_id
            WHERE mem.embedding MATCH ? AND k = ?
            ORDER BY mem.distance ASC
            """,
            (pack_vector(vector), k),
        ).fetchall()
        hits: List[Dict[str, Any]] = []
        for row in rows:
            distance = float(row["score"])
            if cutoff is not None and distance > cutoff:
                continue  # too far away to mean anything (#92), not a hit
            session_id = str(row["session_id"])
            if session_id in ignored or row["origin"] in AUTOMATED_ORIGINS:
                continue
            content = str(row["content"] or "")
            if row["content_hash"] != _content_hash(content):
                continue  # the vector belongs to a replaced body, not this text
            excerpt = content.strip()
            truncated = len(excerpt) > EXCERPT_CHARS
            hits.append(
                _hit(
                    conn,
                    session_id=session_id,
                    seq=int(row["seq"]),
                    role=row["role"],
                    score=distance,
                    excerpt=excerpt[:EXCERPT_CHARS],
                    truncated=truncated,
                )
            )
            if len(hits) >= limit:
                break
        return hits
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _search_messages_fts(
    db: Path,
    query: str,
    limit: int,
    ignored: set,
) -> List[Dict[str, Any]]:
    """Keyword search over message bodies, ranked by bm25 inside the candidates.

    Two passes, both bounded: the session FTS index narrows the archive to at
    most ``FTS_CANDIDATE_SESSIONS`` sessions (its body contains every message
    body, so it cannot miss a session whose message matches), then only those
    sessions' messages are copied into a throwaway ``:memory:`` FTS5 table and
    queried once. Nothing is written to the archive.
    """
    fts_query = build_fts_query(query)
    if fts_query is None:
        return []
    conn = open_connection(db)
    try:
        exclusion = automated_exclusion_sql(conn, "s")
        ignored_sql, ignored_params = _bind_ignored_exclusion(conn, "s.id", ignored)
        candidate_limit = max(FTS_CANDIDATE_SESSIONS, limit * FTS_CANDIDATE_FACTOR)
        candidate_ids = [
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT search_index.entity_id
                FROM search_index
                JOIN sessions s ON s.id = search_index.entity_id
                WHERE search_index MATCH ?
                  AND search_index.entity_type = 'session'{exclusion}{ignored_sql}
                ORDER BY bm25(search_index) ASC
                LIMIT ?
                """,
                (fts_query, *ignored_params, candidate_limit),
            )
        ]
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        conn.execute("ATTACH DATABASE ':memory:' AS message_check")
        conn.execute(
            "CREATE VIRTUAL TABLE message_check.hits USING fts5("
            "session_id UNINDEXED, seq UNINDEXED, role UNINDEXED, body, "
            f"tokenize='{SCOPE_FTS_TOKENIZER}')"
        )
        conn.execute(
            "INSERT INTO message_check.hits (session_id, seq, role, body) "
            "SELECT m.session_id, m.seq, m.role, m.content FROM messages m "
            f"WHERE m.session_id IN ({placeholders})",
            tuple(candidate_ids),
        )
        rows = conn.execute(
            "SELECT session_id, seq, role, body, "
            # A bare table name: a schema prefix is parsed as a column and fails.
            "snippet(hits, 3, ?, ?, '…', 20) AS excerpt, "
            "bm25(hits) AS score "
            "FROM message_check.hits WHERE hits MATCH ? "
            "ORDER BY score ASC, session_id ASC, CAST(seq AS INTEGER) ASC LIMIT ?",
            (SNIPPET_OPEN, SNIPPET_CLOSE, fts_query, max(1, limit)),
        ).fetchall()
        return [
            _hit(
                conn,
                session_id=str(row["session_id"]),
                seq=int(row["seq"]),
                role=row["role"],
                score=float(row["score"]),
                excerpt=snippet_to_text(row["excerpt"] or ""),
                # An elided passage is evidence, not the whole message (#175).
                truncated=_excerpt_elided(row["excerpt"] or "", row["body"] or ""),
            )
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def search_messages(
    output_dir: Path,
    query: str,
    *,
    limit: int = 10,
) -> MessageSearchResult:
    """Find the archived messages that answer ``query``.

    Args:
        output_dir: directory holding ``index_v2.sqlite``.
        query: free-text query; terms are prefix-matched on the FTS path.
        limit: maximum hits returned, clamped to :data:`MAX_LIMIT`.

    Returns:
        The hits (best-match-first, ``score`` smaller-is-better: vec distance on
        the vector path, bm25 on the FTS path) plus the ``path`` that produced
        them. Empty hits — never an exception — when the store is absent, the
        query is empty, or nothing matches.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    text = (query or "").strip()
    db = v2_db_path(output_dir)
    if not text or not db.exists():
        return MessageSearchResult("fts", [])
    ignored = load_ignored_session_ids(output_dir)
    vector_hits = _search_messages_vector(db, text, bounded, ignored)
    if vector_hits:
        return MessageSearchResult("vector", vector_hits)
    return MessageSearchResult("fts", _search_messages_fts(db, text, bounded, ignored))
