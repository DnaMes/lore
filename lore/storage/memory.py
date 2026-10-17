"""Agent memory — the write/recall API for the shared knowledge store (#44 PR 4).

This is the heart of the vision: a place where humans *and* agents record
durable knowledge (facts, decisions, lessons, snippets) that any tool can
later recall. Memory lives in the same v2 SQLite database as the extracted
sessions, and shares the unified ``search_index`` FTS table — so one query
surfaces both past sessions and recorded memory.

Design notes:
- Memory is *append-and-supersede*, not edit-in-place: updating a memory
  inserts a new row and points the old one's ``superseded_by`` at it. This
  keeps an audit trail — you can always see what an agent used to believe.
- ``scope_project`` / ``scope_tool`` let a memory be global or narrowed.
- Every memory gets a row in ``search_index`` (entity_type='memory') so it
  is found by the same search that finds sessions.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .feedback import (
    _check_id,
    _id_in_range,
    feedback_influence,
    ranking_factor,
    weight_from_scores,
)
from .fts import (
    SEARCH_INDEX_BODY_COLUMN,
    SNIPPET_CLOSE,
    SNIPPET_OPEN,
    build_fts_query,
    snippet_to_text,
)
from .fusion import rrf_merge
from .schema import open_v2_connection
from .writer import v2_db_path

logger = logging.getLogger(__name__)

# Recognised memory kinds. Not enforced as a CHECK constraint (forward-compat)
# but the API validates against this set.
MEMORY_KINDS = ("fact", "decision", "todo", "snippet", "link", "lesson", "note")

# Input size caps for add_memory — the memory store is agent-writable, so a
# prompt-injected agent could otherwise bloat the shared DB (#42).
MAX_TITLE_LEN = 512
MAX_BODY_LEN = 64_000
MAX_TAGS = 32
MAX_TAG_LEN = 64
MAX_AUTHOR_LEN = 64

# Review status. Only ``confirmed`` memories are visible by default; ``rejected``
# is reachable only through :func:`set_memory_status`, never at write time.
MEMORY_STATUSES = ("candidate", "confirmed", "rejected")
WRITABLE_STATUSES = ("candidate", "confirmed")

# Auto-mode recall (semantic=None) fuses only semantic candidates whose cosine
# similarity reaches this floor; without one every query would be padded with
# the nearest unrelated memories. Calibrated 2026-10-14 on 61 labelled pairs with
# bge-small-en-v1.5: related median 0.739, unrelated max 0.582 — 0.60 keeps 94 %
# of the related hits and drops every unrelated one (balanced accuracy 0.968,
# tied for the sweep optimum), where the previous 0.7 kept only 77 % for no extra
# protection. Distribution, reproduction command and the two known misses
# (German query vs. English memory, keyword-poor paraphrase):
# docs/research/2026-10-14-recall-floor-calibration.md. The session-level cutoff
# of #92 is calibrated separately. Override with LORE_MEMORY_RECALL_MIN_SIMILARITY.
RECALL_MIN_SIMILARITY_ENV = "LORE_MEMORY_RECALL_MIN_SIMILARITY"
DEFAULT_RECALL_MIN_SIMILARITY = 0.6


# Cosine similarity at or above which a new memory is reported as a near-duplicate
# of an existing one (``find_similar_memories``). A DUPLICATE threshold, distinct
# from the recall floor above and higher on purpose. The default is a PLACEHOLDER
# pending calibration with issue #92. Override with LORE_SIMILARITY_THRESHOLD.
SIMILARITY_THRESHOLD_ENV = "LORE_SIMILARITY_THRESHOLD"
DEFAULT_SIMILARITY_THRESHOLD = 0.85

# Upper bound on how many stored vectors one recall scores in Python. Semantic
# ranking is a ranking aid, not the only retrieval path: memories beyond the
# bound are still found through keywords, and the bound keeps a large archive
# from turning one recall into a full-table vector scan (#199). Override with
# LORE_MEMORY_VECTOR_SCAN_LIMIT.
VECTOR_SCAN_LIMIT_ENV = "LORE_MEMORY_VECTOR_SCAN_LIMIT"
DEFAULT_VECTOR_SCAN_LIMIT = 20_000


def _unit_float_env(name: str, default: float) -> float:
    """``name`` parsed as a float clamped to [0, 1]; ``default`` if unset or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value):
        return default
    return min(1.0, max(0.0, value))


def recall_min_similarity() -> float:
    """The auto-mode similarity floor: env value clamped to [0, 1], default if invalid."""
    return _unit_float_env(RECALL_MIN_SIMILARITY_ENV, DEFAULT_RECALL_MIN_SIMILARITY)


def similarity_threshold() -> float:
    """The near-duplicate threshold: env value clamped to [0, 1], default if invalid."""
    return _unit_float_env(SIMILARITY_THRESHOLD_ENV, DEFAULT_SIMILARITY_THRESHOLD)


def vector_scan_limit() -> int:
    """Cap on memories scored in one recall: env value when a positive int, else the default."""
    raw = os.environ.get(VECTOR_SCAN_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_VECTOR_SCAN_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_VECTOR_SCAN_LIMIT
    return value if value > 0 else DEFAULT_VECTOR_SCAN_LIMIT


def _memory_filter_where(
    *,
    kind: Optional[str],
    scope_project: Optional[str],
    include_superseded: bool,
    status: Optional[str],
) -> Tuple[str, List[Any]]:
    """Shared ``WHERE`` fragment (with leading `` WHERE ``) over the ``m`` alias.

    One definition for the vector check, the vector scan and any caller that
    must agree on which memories a query is about (#199).
    """
    clauses: List[str] = []
    params: List[Any] = []
    if kind:
        clauses.append("m.kind = ?")
        params.append(kind)
    if scope_project:
        clauses.append("m.scope_project = ?")
        params.append(scope_project)
    if not include_superseded:
        clauses.append("m.superseded_by IS NULL")
    if status is not None:
        clauses.append("m.status = ?")
        params.append(status)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


@dataclass
class MemoryEntry:
    """One recalled memory row."""

    id: int
    kind: str
    title: str
    body: str
    author: Optional[str]
    created: str
    updated: str
    scope_project: Optional[str] = None
    scope_tool: Optional[str] = None
    superseded_by: Optional[int] = None
    tags: List[str] = field(default_factory=list)
    # Matching passage with ``«``/``»`` marks; set only by the FTS recall path.
    snippet: Optional[str] = None
    # Sessions this memory came from: ``[{"session_id", "note"}]``. ``message_id``
    # is left out on purpose — ``messages.id`` changes on every full re-sync.
    sources: List[Dict[str, str]] = field(default_factory=list)
    # Derived from memory_feedback ratings; 0.5 / 0 for an unrated memory.
    feedback_weight: float = 0.5
    feedback_count: int = 0
    status: str = "confirmed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "created": self.created,
            "updated": self.updated,
            "scope_project": self.scope_project,
            "scope_tool": self.scope_tool,
            "superseded_by": self.superseded_by,
            "tags": self.tags,
            "snippet": self.snippet,
            "source": "memory",
            "sources": self.sources,
            "feedback_weight": self.feedback_weight,
            "feedback_count": self.feedback_count,
            "status": self.status,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(output_dir: Path) -> sqlite3.Connection:
    """Open the v2 DB (creating + migrating it if needed) for memory ops."""
    conn = open_v2_connection(v2_db_path(output_dir))
    conn.row_factory = sqlite3.Row
    return conn


def add_memory(
    output_dir: Path,
    kind: str,
    title: str,
    body: str,
    *,
    author: str = "human",
    scope_project: Optional[str] = None,
    scope_tool: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    source_session: Optional[str] = None,
    status: str = "confirmed",
    supersedes: Optional[int] = None,
) -> int:
    """Record a new memory and index it for search. Returns the new memory id.

    ``supersedes`` points an older memory's ``superseded_by`` at the new row in the
    same transaction as the insert. The target must exist, be confirmed, not yet
    superseded and have the same ``scope_project``; a different ``kind`` is fine.
    Any violation raises ``ValueError`` and nothing is written.

    ``status`` is ``confirmed`` (default, visible) or ``candidate`` (hidden
    until reviewed); ``rejected`` is only reachable via :func:`set_memory_status`.

    When ``source_session`` is given, a row is recorded in ``memory_sources``
    linking the memory to the session it was derived from — so the memory's
    provenance ("this came from session X") is traceable (#33).

    Raises:
        ValueError: if ``kind`` is unknown, ``title``/``body`` are empty, or
            an input exceeds its size cap (#42), or ``status`` is not writable,
            or ``supersedes`` is not a valid id, or ``metadata`` is not JSON.
    """
    if status not in WRITABLE_STATUSES:
        raise ValueError(f"invalid memory status {status!r}; expected one of {WRITABLE_STATUSES}")
    if supersedes is not None:
        _check_id(supersedes, "supersedes")
    if supersedes is not None and status != "confirmed":
        raise ValueError("only a confirmed memory can supersede another")
    if kind not in MEMORY_KINDS:
        raise ValueError(f"unknown memory kind {kind!r}; expected one of {MEMORY_KINDS}")
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise ValueError("memory title must not be empty")
    if not body:
        raise ValueError("memory body must not be empty")
    # Size caps — the store is agent-writable; reject oversized input (#42).
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"memory title exceeds {MAX_TITLE_LEN} chars")
    if len(body) > MAX_BODY_LEN:
        raise ValueError(f"memory body exceeds {MAX_BODY_LEN} chars")
    author = (author or "human").strip()[:MAX_AUTHOR_LEN]
    if tags:
        clean_tags = [t.strip() for t in tags if t and t.strip()]
        if len(clean_tags) > MAX_TAGS:
            raise ValueError(f"too many tags (max {MAX_TAGS})")
        if any(len(t) > MAX_TAG_LEN for t in clean_tags):
            raise ValueError(f"a tag exceeds {MAX_TAG_LEN} chars")
        tags = clean_tags

    try:
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-serializable") from exc

    now = _now()
    conn = _connect(output_dir)
    try:
        # IMMEDIATE: take the write lock before validating the supersedes target.
        conn.execute("BEGIN IMMEDIATE")
        if supersedes is not None:
            _check_supersedable(conn, supersedes, scope_project)
        cur = conn.execute(
            """
            INSERT INTO memory (
                kind, title, body, scope_project, scope_tool, author,
                created, updated, superseded_by, metadata_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                kind,
                title,
                body,
                scope_project,
                scope_tool,
                author,
                now,
                now,
                metadata_json,
                status,
            ),
        )
        memory_id = int(cur.lastrowid or 0)

        for tag in {t.strip() for t in (tags or []) if t and t.strip()}:
            conn.execute(
                "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
                (memory_id, tag),
            )

        conn.execute(
            """
            INSERT INTO search_index (entity_type, entity_id, tool, project, title, body)
            VALUES ('memory', ?, ?, ?, ?, ?)
            """,
            (str(memory_id), scope_tool or "", scope_project or "", title, body),
        )
        if supersedes is not None:
            conn.execute(
                "UPDATE memory SET superseded_by = ?, updated = ? WHERE id = ?",
                (memory_id, now, supersedes),
            )
        # Provenance (#33), in the same transaction: it commits or rolls back with the memory.
        if source_session:
            conn.execute(
                "INSERT OR IGNORE INTO memory_sources (memory_id, session_id, message_id, note) "
                "VALUES (?, ?, NULL, 'recorded from this session')",
                (memory_id, source_session),
            )
        conn.execute("COMMIT")
    except (sqlite3.Error, ValueError):
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

    # Embed the memory for semantic recall — best-effort, in its own
    # connection. A missing embedding backend or an embedding failure must
    # never lose the memory, which is already committed above.
    _store_embedding(output_dir, memory_id, f"{title}\n{body}")
    return memory_id


def _check_supersedable(
    conn: sqlite3.Connection, target_id: int, scope_project: Optional[str]
) -> None:
    """Raise ``ValueError`` unless ``target_id`` may be superseded by a new memory."""
    row = conn.execute(
        "SELECT status, superseded_by, scope_project FROM memory WHERE id = ?", (target_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"memory {target_id} does not exist")
    if row["status"] != "confirmed":
        raise ValueError(f"memory {target_id} is {row['status']}, not confirmed")
    if row["superseded_by"] is not None:
        raise ValueError(f"memory {target_id} is already superseded by #{row['superseded_by']}")
    if row["scope_project"] != scope_project:
        raise ValueError(f"memory {target_id} has a different project scope")


def find_similar_memories(
    output_dir: Path,
    text: str,
    *,
    scope_project: Optional[str],
    limit: int = 3,
    exclude_id: Optional[int] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Active, confirmed memories in the same scope that nearly duplicate ``text``.

    Returns ``[{"id", "title", "similarity"}]`` best first, at or above
    :func:`similarity_threshold`, or None when no embedding backend is available.
    Call it BEFORE inserting the new memory so it cannot match itself; pass the id
    of a memory about to be superseded as ``exclude_id`` to leave it out.
    """
    from .embeddings import cosine_similarity, embed_text, unpack_vector

    vector = embed_text(text)
    if vector is None:
        return None
    threshold = similarity_threshold()
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.title, e.vector
            FROM memory m
            JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE m.superseded_by IS NULL AND m.status = 'confirmed'
              AND m.scope_project IS ?
            """,
            (scope_project,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    scored = [
        {
            "id": int(r["id"]),
            "title": r["title"],
            "similarity": cosine_similarity(vector, unpack_vector(r["vector"])),
        }
        for r in rows
    ]
    hits = [h for h in scored if h["similarity"] >= threshold and h["id"] != exclude_id]
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[: max(0, limit)]


def embed_missing_memories(output_dir: Path, *, limit: int = 500) -> int:
    """Backfill embeddings for memories that have no stored vector (best-effort).

    Skips ``rejected`` rows. Returns how many embeddings were stored.
    """
    from .embeddings import embeddings_available

    if not embeddings_available():
        return 0
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.title, m.body
            FROM memory m
            LEFT JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE e.memory_id IS NULL AND m.status != 'rejected'
            ORDER BY m.id
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    stored = 0
    for row in rows:
        _store_embedding(output_dir, int(row["id"]), f"{row['title']}\n{row['body']}")
        stored += 1
    return stored


def _store_embedding(output_dir: Path, memory_id: int, text: str) -> None:
    """Compute and store an embedding for a memory. Silent no-op if the
    optional embedding backend is unavailable (#33)."""
    from .embeddings import DEFAULT_MODEL, embed_text, pack_vector

    vector = embed_text(text)
    if vector is None:
        return
    conn = _connect(output_dir)
    try:
        conn.execute(
            """
            INSERT INTO memory_embeddings (memory_id, model, dim, vector, created)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                model = excluded.model, dim = excluded.dim,
                vector = excluded.vector, created = excluded.created
            """,
            (memory_id, DEFAULT_MODEL, len(vector), pack_vector(vector), _now()),
        )
    except sqlite3.Error as exc:
        logger.warning("Could not store embedding for memory %s: %s", memory_id, exc)
    finally:
        conn.close()


# Stay well under SQLite's bound-variable limit (999 on older builds).
_IN_CHUNK = 500


def _load_entries(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> List[MemoryEntry]:
    """Build entries for ``rows`` with tags, sources and feedback batch-loaded.

    Pass only rows that survived filtering: each relation costs one ``IN (...)``
    query per chunk of ids, not one per row.
    """
    ids = [row["id"] for row in rows]
    tags: Dict[int, List[str]] = {i: [] for i in ids}
    sources: Dict[int, List[Dict[str, str]]] = {i: [] for i in ids}
    scores: Dict[int, List[int]] = {i: [] for i in ids}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT memory_id, tag FROM memory_tags WHERE memory_id IN ({marks}) ORDER BY tag",
            chunk,
        ):
            tags[r["memory_id"]].append(r["tag"])
        for r in conn.execute(
            "SELECT memory_id, session_id, note FROM memory_sources "
            f"WHERE memory_id IN ({marks}) ORDER BY session_id",
            chunk,
        ):
            sources[r["memory_id"]].append({"session_id": r["session_id"], "note": r["note"] or ""})
        for r in conn.execute(
            f"SELECT memory_id, score FROM memory_feedback WHERE memory_id IN ({marks})",
            chunk,
        ):
            scores[r["memory_id"]].append(r["score"])

    entries: List[MemoryEntry] = []
    for row in rows:
        weight, count = weight_from_scores(scores[row["id"]])
        entries.append(
            MemoryEntry(
                id=row["id"],
                kind=row["kind"],
                title=row["title"],
                body=row["body"],
                author=row["author"],
                created=row["created"],
                updated=row["updated"],
                scope_project=row["scope_project"],
                scope_tool=row["scope_tool"],
                superseded_by=row["superseded_by"],
                tags=tags[row["id"]],
                sources=sources[row["id"]],
                feedback_weight=weight,
                feedback_count=count,
                status=row["status"],
            )
        )
    return entries


def get_memory(output_dir: Path, memory_id: int) -> Optional[MemoryEntry]:
    """Return a single memory by id, or None if it does not exist."""
    if not _id_in_range(memory_id):
        return None
    conn = _connect(output_dir)
    try:
        row = conn.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
        return _load_entries(conn, [row])[0] if row else None
    finally:
        conn.close()


def list_memory(
    output_dir: Path,
    *,
    kind: Optional[str] = None,
    scope_project: Optional[str] = None,
    include_superseded: bool = False,
    limit: int = 50,
    status: Optional[str] = "confirmed",
) -> List[MemoryEntry]:
    """List memory entries, newest first, with optional filters.

    ``status`` defaults to confirmed memories only; ``None`` means any status.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if scope_project:
        clauses.append("scope_project = ?")
        params.append(scope_project)
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = _connect(output_dir)
    try:
        # id DESC is the tiebreaker — timestamps are second-resolution, so
        # two memories added in the same second must still order by insertion.
        rows = conn.execute(
            f"SELECT * FROM memory{where} ORDER BY updated DESC, id DESC LIMIT ?",
            (*params, max(1, limit)),
        ).fetchall()
        return _load_entries(conn, rows)
    finally:
        conn.close()


def _semantic_candidates(
    output_dir: Path,
    query: str,
    *,
    kind: Optional[str],
    scope_project: Optional[str],
    include_superseded: bool,
    limit: int,
    status: Optional[str] = "confirmed",
    min_similarity: Optional[float] = None,
) -> Optional[List[Tuple[MemoryEntry, float]]]:
    """Rank memories by embedding cosine similarity to the query.

    Returns ``(entry, similarity)`` pairs, best first; candidates below
    ``min_similarity`` (when given) are dropped. Returns None when semantic
    search is unavailable (no embedding backend, or no embeddings stored yet)
    so the caller falls back to FTS.
    """
    from .embeddings import cosine_similarity, embed_text, unpack_vector

    query_vec = embed_text(query)
    if query_vec is None:
        return None

    where, where_params = _memory_filter_where(
        kind=kind,
        scope_project=scope_project,
        include_superseded=include_superseded,
        status=status,
    )
    conn = _connect(output_dir)
    try:
        bound = vector_scan_limit()
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory m JOIN memory_embeddings e ON e.memory_id = m.id"
                + where,
                where_params,
            ).fetchone()[0]
        )
        if total > bound:
            logger.warning(
                "memory semantic scan scores %d of %d embedded memories (%s=%d); "
                "the rest are reachable through keywords only",
                bound,
                total,
                VECTOR_SCAN_LIMIT_ENV,
                bound,
            )
        # Filters are applied in SQL so the bound is spent on candidates the
        # query can actually return; newest first, so the bound is deterministic.
        rows = conn.execute(
            "SELECT m.*, e.vector AS _vec "
            "FROM memory m JOIN memory_embeddings e ON e.memory_id = m.id"
            + where
            + " ORDER BY m.updated DESC, m.id DESC LIMIT ?",
            (*where_params, bound),
        ).fetchall()
        if not rows:
            return None  # nothing embedded for this query — fall back to FTS

        scored = []
        for row in rows:
            score = cosine_similarity(query_vec, unpack_vector(row["_vec"]))
            if min_similarity is not None and score < min_similarity:
                continue
            scored.append((score, row))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:limit]
        entries = _load_entries(conn, [row for _score, row in top])
        return [(entry, score) for entry, (score, _row) in zip(entries, top)]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _semantic_recall(
    output_dir: Path,
    query: str,
    *,
    kind: Optional[str],
    scope_project: Optional[str],
    include_superseded: bool,
    limit: int,
    status: Optional[str],
) -> Optional[List[MemoryEntry]]:
    """Entries of :func:`_semantic_candidates` without a similarity floor."""
    pairs = _semantic_candidates(
        output_dir,
        query,
        kind=kind,
        scope_project=scope_project,
        include_superseded=include_superseded,
        limit=limit,
        status=status,
    )
    return None if pairs is None else [entry for entry, _score in pairs]


def _has_stored_vectors(
    output_dir: Path,
    *,
    kind: Optional[str] = None,
    scope_project: Optional[str] = None,
    include_superseded: bool = False,
    status: Optional[str] = "confirmed",
) -> bool:
    """True when the backend is importable and a memory *this query can return* has a vector.

    The check takes the query's filters (#199): auto mode used to load the
    embedder for a ``kind``/``scope_project`` whose memories are keyword-only.
    """
    from .embeddings import embeddings_available

    if not embeddings_available():
        return False
    where, params = _memory_filter_where(
        kind=kind,
        scope_project=scope_project,
        include_superseded=include_superseded,
        status=status,
    )
    conn = _connect(output_dir)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM memory m JOIN memory_embeddings e ON e.memory_id = m.id"
                + where
                + " LIMIT 1",
                params,
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _fts_recall(
    output_dir: Path,
    fts_query: str,
    *,
    kind: Optional[str],
    scope_project: Optional[str],
    include_superseded: bool,
    limit: int,
    status: Optional[str],
) -> List[MemoryEntry]:
    """Keyword recall over the shared ``search_index`` table, best first."""
    # Filtered in SQL (unlike kind/scope below) so candidates cannot crowd the
    # over-fetch window out of confirmed hits.
    status_clause = "AND m.status = ?" if status is not None else ""
    status_params: Tuple[Any, ...] = (status,) if status is not None else ()
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            f"""
            SELECT m.*, bm25(search_index) AS score,
                   snippet(search_index, {SEARCH_INDEX_BODY_COLUMN}, ?, ?, '…', 24) AS snippet
            FROM search_index
            JOIN memory m ON m.id = CAST(search_index.entity_id AS INTEGER)
            WHERE search_index MATCH ?
              AND search_index.entity_type = 'memory'
              {status_clause}
            ORDER BY score ASC
            LIMIT ?
            """,
            (SNIPPET_OPEN, SNIPPET_CLOSE, fts_query, *status_params, max(1, limit) * 4),
        ).fetchall()

        kept: List[sqlite3.Row] = []
        for row in rows:
            if kind and row["kind"] != kind:
                continue
            if scope_project and row["scope_project"] != scope_project:
                continue
            if not include_superseded and row["superseded_by"] is not None:
                continue
            kept.append(row)
            if len(kept) >= limit:
                break
        entries = _load_entries(conn, kept)
        for entry, row in zip(entries, kept):
            if row["snippet"]:
                entry.snippet = snippet_to_text(row["snippet"])
        return entries
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _apply_feedback_ranking(entries: List[MemoryEntry], influence: float) -> List[MemoryEntry]:
    """Reorder by ``1 / (60 + rank)`` times the feedback factor; stable on ties."""
    scored = [
        (ranking_factor(e.feedback_weight, influence) / (60 + rank), e)
        for rank, e in enumerate(entries, start=1)
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [e for _score, e in scored]


def recall_memory(
    output_dir: Path,
    query: str,
    *,
    kind: Optional[str] = None,
    scope_project: Optional[str] = None,
    include_superseded: bool = False,
    limit: int = 10,
    semantic: Optional[bool] = None,
    status: Optional[str] = "confirmed",
) -> List[MemoryEntry]:
    """Search recorded memory; see :func:`_recall` for the retrieval modes.

    ``status`` defaults to confirmed memories only; ``None`` means any status.

    Only when ``LORE_FEEDBACK_INFLUENCE`` is above 0 are the results reordered
    by their feedback weight (bounded to +-25%); otherwise the order is the
    retrieval order, untouched.
    """
    entries = _recall(
        output_dir,
        query,
        kind=kind,
        scope_project=scope_project,
        include_superseded=include_superseded,
        limit=limit,
        semantic=semantic,
        status=status,
    )
    influence = feedback_influence()
    if influence > 0 and len(entries) > 1:
        return _apply_feedback_ranking(entries, influence)
    return entries


def _recall(
    output_dir: Path,
    query: str,
    *,
    kind: Optional[str],
    scope_project: Optional[str],
    include_superseded: bool,
    limit: int,
    semantic: Optional[bool],
    status: Optional[str],
) -> List[MemoryEntry]:
    """Search recorded memory.

    ``semantic`` is a tri-state:

    - ``None`` (auto): keyword search (only, for a quoted phrase); when the
      optional embedding backend is
      installed and a vector is stored for a memory this query can return
      (same kind/scope/status), also semantic candidates at or above
      :func:`recall_min_similarity`, fused with keyword hits by RRF. Memories
      without a vector are still found through keywords.
    - ``False``: keyword (FTS) search only; the embedder is never touched.
    - ``True``: rank by embedding similarity without a floor, falling back to
      FTS when the backend or stored vectors are missing.

    An empty query always falls back to :func:`list_memory`.
    """
    fts_query = build_fts_query(query)
    if fts_query is None:
        return list_memory(
            output_dir,
            kind=kind,
            scope_project=scope_project,
            include_superseded=include_superseded,
            limit=limit,
            status=status,
        )

    filters: Dict[str, Any] = {
        "kind": kind,
        "scope_project": scope_project,
        "include_superseded": include_superseded,
        "limit": limit,
        "status": status,
    }
    if semantic:
        result = _semantic_recall(output_dir, query, **filters)
        if result is not None:
            return result
        # else: fall through to FTS

    # Terms are prefix-matched ("oauth" should also find "OAuth2"): FTS5's
    # unicode61 tokenizer keeps digits in a token, so an exact match would
    # miss them. A fully quoted query is one phrase (see build_fts_query).
    fts_entries = _fts_recall(output_dir, fts_query, **filters)
    # A quoted phrase asks for that exact text: build_fts_query ends a phrase with
    # '"' and every term query with '*'. Fusing similar-meaning memories would
    # break that contract, so auto mode stays keyword-only for phrases.
    if (
        semantic is not None
        or fts_query.endswith('"')
        or not _has_stored_vectors(
            output_dir,
            kind=kind,
            scope_project=scope_project,
            include_superseded=include_superseded,
            status=status,
        )
    ):
        return fts_entries

    pairs = _semantic_candidates(
        output_dir, query, min_similarity=recall_min_similarity(), **filters
    )
    if not pairs:
        return fts_entries
    fts_results = [{"id": e.id, "entry": e, "score": 0.0} for e in fts_entries]
    semantic_results = [{"id": e.id, "entry": e, "score": s} for e, s in pairs]
    fused = rrf_merge(fts_results, semantic_results, key=lambda r: str(r["id"]), limit=limit)
    return [r["entry"] for r in fused]


def supersede_memory(
    output_dir: Path,
    old_id: int,
    title: str,
    body: str,
    *,
    author: str = "human",
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """Replace a memory with an updated version, preserving the audit trail.

    Inserts a new memory (same kind/scope as the old one) and points the old
    row's ``superseded_by`` at it in one transaction (see :func:`add_memory`).
    Returns the new memory id.

    Raises:
        ValueError: if ``old_id`` does not exist, is not confirmed or is
            already superseded.
    """
    old = get_memory(output_dir, old_id)
    if old is None:
        raise ValueError(f"memory {old_id} does not exist")
    return add_memory(
        output_dir,
        kind=old.kind,
        title=title,
        body=body,
        author=author,
        scope_project=old.scope_project,
        scope_tool=old.scope_tool,
        tags=tags if tags is not None else old.tags,
        metadata=metadata,
        supersedes=old_id,
    )


_SETTABLE_FROM_STATUSES = MEMORY_STATUSES


def set_memory_status(
    output_dir: Path, memory_id: int, status: str, *, from_status: str = "candidate"
) -> bool:
    """Review a candidate, or explicitly reject/restore a duplicate via ``from_status``.

    Rejected rows are kept, never deleted. Returns True when the status changed
    and False when the memory does not exist.

    Raises:
        ValueError: if ``status`` is not ``confirmed``/``rejected``, ``from_status``
            is not ``candidate``/``confirmed``/``rejected``, or the memory is not
            currently in the expected source status.
    """
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"invalid target status {status!r}; expected 'confirmed' or 'rejected'")
    if from_status not in _SETTABLE_FROM_STATUSES:
        raise ValueError(
            f"invalid source status {from_status!r}; expected one of {_SETTABLE_FROM_STATUSES}"
        )
    if from_status == status:
        raise ValueError(f"invalid source status {from_status!r} for target {status!r}")
    if not _id_in_range(memory_id):
        return False
    conn = _connect(output_dir)
    try:
        cur = conn.execute(
            "UPDATE memory SET status = ?, updated = ? WHERE id = ? AND status = ?",
            (status, _now(), memory_id, from_status),
        )
        if cur.rowcount > 0:
            return True
        row = conn.execute("SELECT status FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return False
        raise ValueError(f"memory {memory_id} is {row['status']}, expected {from_status}")
    finally:
        conn.close()


def reject_memories_bulk(output_dir: Path, memory_ids: List[int]) -> int:
    """Mark active confirmed memories rejected in one transaction.

    Every id must exist, be ``confirmed``, and not superseded. Raises
    ``ValueError`` with details on missing or changed ids; no row is updated
    on failure.
    """
    if not memory_ids:
        return 0
    unique_ids = list(dict.fromkeys(memory_ids))
    for memory_id in unique_ids:
        if not _id_in_range(memory_id):
            raise ValueError(f"memory id {memory_id} out of range")

    conn = _connect(output_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" * len(unique_ids))
        rows = conn.execute(
            f"SELECT id, status, superseded_by FROM memory WHERE id IN ({marks})",
            unique_ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        missing = [memory_id for memory_id in unique_ids if memory_id not in by_id]
        not_confirmed: List[str] = []
        for memory_id in unique_ids:
            if memory_id not in by_id:
                continue
            row = by_id[memory_id]
            if row["status"] != "confirmed" or row["superseded_by"] is not None:
                detail = f"#{memory_id} is {row['status']!r}"
                if row["superseded_by"] is not None:
                    detail += f" (superseded_by=#{row['superseded_by']})"
                not_confirmed.append(detail)
        if missing or not_confirmed:
            parts: List[str] = []
            if missing:
                parts.append("missing: " + ", ".join(f"#{memory_id}" for memory_id in missing))
            if not_confirmed:
                parts.append("not active confirmed: " + ", ".join(not_confirmed))
            raise ValueError("; ".join(parts))

        now = _now()
        updated = 0
        for memory_id in unique_ids:
            cur = conn.execute(
                "UPDATE memory SET status = 'rejected', updated = ? "
                "WHERE id = ? AND status = 'confirmed' AND superseded_by IS NULL",
                (now, memory_id),
            )
            updated += cur.rowcount
        if updated != len(unique_ids):
            raise ValueError(
                f"concurrent change: expected to reject {len(unique_ids)} memories, updated {updated}"
            )
        conn.execute("COMMIT")
        return updated
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def restore_rejected_memories_bulk(output_dir: Path, memory_ids: List[int]) -> Tuple[int, int, int]:
    """Set still-rejected, non-superseded ids back to confirmed.

    Returns (restored, skipped, missing). Reads and writes share one transaction.
    """
    if not memory_ids:
        return (0, 0, 0)
    unique_ids = list(dict.fromkeys(memory_ids))
    conn = _connect(output_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" * len(unique_ids))
        rows = conn.execute(f"SELECT id FROM memory WHERE id IN ({marks})", unique_ids).fetchall()
        missing = len(unique_ids) - len(rows)
        now = _now()
        restored = 0
        for memory_id in unique_ids:
            cur = conn.execute(
                "UPDATE memory SET status = 'confirmed', updated = ? "
                "WHERE id = ? AND status = 'rejected' AND superseded_by IS NULL",
                (now, memory_id),
            )
            restored += cur.rowcount
        conn.execute("COMMIT")
        return (restored, len(unique_ids) - restored - missing, missing)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def list_superseded_by(output_dir: Path, memory_id: int) -> List[int]:
    """Ids of memories whose ``superseded_by`` points at ``memory_id``."""
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            "SELECT id FROM memory WHERE superseded_by = ? ORDER BY id", (memory_id,)
        ).fetchall()
        return [int(row["id"]) for row in rows]
    finally:
        conn.close()


def link_memory_to_session(
    output_dir: Path,
    memory_id: int,
    session_id: str,
    *,
    message_id: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    """Record that a memory originated from a particular session/message."""
    conn = _connect(output_dir)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_sources (memory_id, session_id, message_id, note)
            VALUES (?, ?, ?, ?)
            """,
            (memory_id, session_id, message_id, note),
        )
    finally:
        conn.close()


def list_memory_sources(output_dir: Path, memory_id: int) -> List[Dict[str, Any]]:
    """Return the source-session links recorded for a memory (#33).

    Each entry is ``{"session_id", "message_id", "note"}`` — the sessions
    a memory was derived from. Empty list when nothing is linked.
    """
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            "SELECT session_id, message_id, note FROM memory_sources "
            "WHERE memory_id = ? ORDER BY session_id",
            (memory_id,),
        ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "message_id": r["message_id"],
                "note": r["note"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def delete_memory(output_dir: Path, memory_id: int) -> bool:
    """Hard-delete a memory and its FTS/tag/source rows. Returns True if removed."""
    if not _id_in_range(memory_id):
        return False
    conn = _connect(output_dir)
    try:
        conn.execute("BEGIN")
        cur = conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
        conn.execute(
            "DELETE FROM search_index WHERE entity_type = 'memory' AND entity_id = ?",
            (str(memory_id),),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()
