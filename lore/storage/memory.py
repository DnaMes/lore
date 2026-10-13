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

from .fts import SNIPPET_CLOSE, SNIPPET_OPEN, build_fts_query, snippet_to_text
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

# Auto-mode recall (semantic=None) fuses only semantic candidates whose cosine
# similarity reaches this floor; without one every query would be padded with
# the nearest unrelated memories. The default is a PLACEHOLDER, to be calibrated
# together with issue #92: a handful of short texts with bge-small-en-v1.5
# (2026-10-13) gave about 0.76 for related and 0.45-0.62 for unrelated pairs.
# Override with LORE_MEMORY_RECALL_MIN_SIMILARITY.
RECALL_MIN_SIMILARITY_ENV = "LORE_MEMORY_RECALL_MIN_SIMILARITY"
DEFAULT_RECALL_MIN_SIMILARITY = 0.7


def recall_min_similarity() -> float:
    """The auto-mode similarity floor: env value clamped to [0, 1], default if invalid."""
    raw = os.environ.get(RECALL_MIN_SIMILARITY_ENV, "").strip()
    if not raw:
        return DEFAULT_RECALL_MIN_SIMILARITY
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RECALL_MIN_SIMILARITY
    if math.isnan(value):
        return DEFAULT_RECALL_MIN_SIMILARITY
    return min(1.0, max(0.0, value))


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
) -> int:
    """Record a new memory and index it for search. Returns the new memory id.

    When ``source_session`` is given, a row is recorded in ``memory_sources``
    linking the memory to the session it was derived from — so the memory's
    provenance ("this came from session X") is traceable (#33).

    Raises:
        ValueError: if ``kind`` is unknown, ``title``/``body`` are empty, or
            an input exceeds its size cap (#42).
    """
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

    now = _now()
    conn = _connect(output_dir)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            """
            INSERT INTO memory (
                kind, title, body, scope_project, scope_tool, author,
                created, updated, superseded_by, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
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
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

    # Link the memory to its source session, if one was given (#33). This
    # records provenance — done after the memory commits, best-effort.
    if source_session:
        link_memory_to_session(
            output_dir, memory_id, source_session, note="recorded from this session"
        )

    # Embed the memory for semantic recall — best-effort, in its own
    # connection. A missing embedding backend or an embedding failure must
    # never lose the memory, which is already committed above.
    _store_embedding(output_dir, memory_id, f"{title}\n{body}")
    return memory_id


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


def _load_tags(conn: sqlite3.Connection, memory_id: int) -> List[str]:
    rows = conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (memory_id,)
    ).fetchall()
    return [r["tag"] for r in rows]


def _load_sources(conn: sqlite3.Connection, memory_id: int) -> List[Dict[str, str]]:
    rows = conn.execute(
        "SELECT session_id, note FROM memory_sources WHERE memory_id = ? ORDER BY session_id",
        (memory_id,),
    ).fetchall()
    return [{"session_id": r["session_id"], "note": r["note"] or ""} for r in rows]


def _row_to_entry(conn: sqlite3.Connection, row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
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
        tags=_load_tags(conn, row["id"]),
        sources=_load_sources(conn, row["id"]),
    )


def get_memory(output_dir: Path, memory_id: int) -> Optional[MemoryEntry]:
    """Return a single memory by id, or None if it does not exist."""
    conn = _connect(output_dir)
    try:
        row = conn.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_entry(conn, row) if row else None
    finally:
        conn.close()


def list_memory(
    output_dir: Path,
    *,
    kind: Optional[str] = None,
    scope_project: Optional[str] = None,
    include_superseded: bool = False,
    limit: int = 50,
) -> List[MemoryEntry]:
    """List memory entries, newest first, with optional filters."""
    clauses: List[str] = []
    params: List[Any] = []
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
        return [_row_to_entry(conn, r) for r in rows]
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

    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.*, e.vector AS _vec
            FROM memory m
            JOIN memory_embeddings e ON e.memory_id = m.id
            """
        ).fetchall()
        if not rows:
            return None  # nothing embedded yet — fall back to FTS

        scored = []
        for row in rows:
            if kind and row["kind"] != kind:
                continue
            if scope_project and row["scope_project"] != scope_project:
                continue
            if not include_superseded and row["superseded_by"] is not None:
                continue
            score = cosine_similarity(query_vec, unpack_vector(row["_vec"]))
            if min_similarity is not None and score < min_similarity:
                continue
            scored.append((score, row))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(_row_to_entry(conn, row), score) for score, row in scored[:limit]]
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
) -> Optional[List[MemoryEntry]]:
    """Entries of :func:`_semantic_candidates` without a similarity floor."""
    pairs = _semantic_candidates(
        output_dir,
        query,
        kind=kind,
        scope_project=scope_project,
        include_superseded=include_superseded,
        limit=limit,
    )
    return None if pairs is None else [entry for entry, _score in pairs]


def _has_stored_vectors(output_dir: Path) -> bool:
    """True when the embedding backend is importable and a memory has a vector."""
    from .embeddings import embeddings_available

    if not embeddings_available():
        return False
    conn = _connect(output_dir)
    try:
        return conn.execute("SELECT 1 FROM memory_embeddings LIMIT 1").fetchone() is not None
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
) -> List[MemoryEntry]:
    """Keyword recall over the shared ``search_index`` table, best first."""
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.*, bm25(search_index) AS score,
                   snippet(search_index, 5, ?, ?, '…', 24) AS snippet
            FROM search_index
            JOIN memory m ON m.id = CAST(search_index.entity_id AS INTEGER)
            WHERE search_index MATCH ?
              AND search_index.entity_type = 'memory'
            ORDER BY score ASC
            LIMIT ?
            """,
            (SNIPPET_OPEN, SNIPPET_CLOSE, fts_query, max(1, limit) * 4),
        ).fetchall()

        entries: List[MemoryEntry] = []
        for row in rows:
            if kind and row["kind"] != kind:
                continue
            if scope_project and row["scope_project"] != scope_project:
                continue
            if not include_superseded and row["superseded_by"] is not None:
                continue
            entry = _row_to_entry(conn, row)
            if row["snippet"]:
                entry.snippet = snippet_to_text(row["snippet"])
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def recall_memory(
    output_dir: Path,
    query: str,
    *,
    kind: Optional[str] = None,
    scope_project: Optional[str] = None,
    include_superseded: bool = False,
    limit: int = 10,
    semantic: Optional[bool] = None,
) -> List[MemoryEntry]:
    """Search recorded memory.

    ``semantic`` is a tri-state:

    - ``None`` (auto): keyword search (only, for a quoted phrase); when the
      optional embedding backend is
      installed and a vector is stored, also semantic candidates at or above
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
        )

    filters: Dict[str, Any] = {
        "kind": kind,
        "scope_project": scope_project,
        "include_superseded": include_superseded,
        "limit": limit,
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
    if semantic is not None or fts_query.endswith('"') or not _has_stored_vectors(output_dir):
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
    row's ``superseded_by`` at it. Returns the new memory id.

    Raises:
        ValueError: if ``old_id`` does not exist.
    """
    old = get_memory(output_dir, old_id)
    if old is None:
        raise ValueError(f"memory {old_id} does not exist")

    new_id = add_memory(
        output_dir,
        kind=old.kind,
        title=title,
        body=body,
        author=author,
        scope_project=old.scope_project,
        scope_tool=old.scope_tool,
        tags=tags if tags is not None else old.tags,
        metadata=metadata,
    )

    # Point the old row at the new one. add_memory has already committed the
    # new memory, so if this UPDATE fails we compensate by deleting the new
    # memory — leaving the store as it was rather than with two unlinked,
    # non-superseded memories.
    update_ok = False
    conn = _connect(output_dir)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE memory SET superseded_by = ?, updated = ? WHERE id = ?",
            (new_id, _now(), old_id),
        )
        conn.execute("COMMIT")
        update_ok = True
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()
        if not update_ok:
            # The superseded_by link failed — delete the orphan new memory
            # so the store is unchanged rather than holding two unlinked rows.
            delete_memory(output_dir, new_id)
    return new_id


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
