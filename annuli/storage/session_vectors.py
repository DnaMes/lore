"""Populate per-session embedding vectors for hybrid search (#87, PR 2; #95).

One vector per session, stored in the ``session_embeddings`` vec0 table
(created by :func:`lore.storage.schema.ensure_session_vec_table`). The text
embedded is the same title + message-body concatenation that feeds the FTS
``search_index`` row, so keyword and semantic search see the same content.

Since #95 the pass is **incremental**: ``session_embedding_meta`` (migration
13) records which ``source_mtime_ns`` + model each stored vector was computed
from, and only new/changed sessions are re-embedded. A full re-embed of the
archive (~950 CPU model calls) blew the web reload job's 180s timeout; the
common-case delta is a handful of sessions and takes well under a second.
A **time budget** (``LORE_EMBED_BUDGET_SECONDS``, default 60, ``0`` =
unlimited) bounds the worst case (first backfill): progress is committed and
the remainder is picked up by the next reload.

Everything here is **best-effort**: when sqlite-vec or fastembed is missing —
or an individual embed fails — the affected sessions simply get no vector and
search falls back to FTS. A vector failure must never break the index write,
so this runs in its own transaction *after* the main ``write_sessions`` commit,
never inside it (embedding is slow and would hold the write lock).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from .embeddings import DEFAULT_MODEL, embed_text, embeddings_available, pack_vector
from .schema import ensure_session_vec_table

logger = logging.getLogger(__name__)

# fastembed's bge-small truncates well before this, but cap the input so a
# pathological multi-megabyte session doesn't spend seconds tokenising text the
# model will discard anyway. Roughly a few thousand tokens of head context.
_MAX_EMBED_CHARS = 20_000

# (session_id, text to embed or None, source_mtime_ns or None).
# text None = "keep-only": the session still exists (e.g. a metadata-only
# reused entry from incremental sync) — protect its stored vector from the
# vanished-session cleanup, but never (re)embed it.
SessionText = Tuple[str, Optional[str], Optional[int]]


def _embed_budget_seconds() -> Optional[float]:
    """Per-pass time budget from ``LORE_EMBED_BUDGET_SECONDS`` (default 60s).

    ``0`` (or a negative value) disables the budget — used by one-shot CLI
    backfills where wall time doesn't race a job timeout.
    """
    raw = os.environ.get("LORE_EMBED_BUDGET_SECONDS", "60").strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid LORE_EMBED_BUDGET_SECONDS=%r, using 60", raw)
        value = 60.0
    return value if value > 0 else None


def embed_sessions(
    conn: sqlite3.Connection,
    items: Iterable[SessionText],
    *,
    budget_seconds: Optional[float] = None,
) -> int:
    """Incrementally sync ``session_embeddings`` to the given sessions.

    - Sessions absent from ``items`` are removed from the vector store.
    - Sessions whose ``source_mtime_ns`` (or the embedding model) changed —
      or that have no stored vector yet — are (re)embedded, newest state wins.
    - Unchanged sessions are skipped entirely; keep-only items (text=None)
      are never embedded, only protected from cleanup.
    - Stops early once ``budget_seconds`` is spent (default from
      ``LORE_EMBED_BUDGET_SECONDS``); committed progress is kept and the next
      pass continues where this one stopped.

    Returns the number of vectors written. Returns 0 (without raising) when
    the vector table or the embedding backend is unavailable. Duplicate
    session ids are deduped (last wins) — vec0 rejects duplicate keys even
    under ``INSERT OR REPLACE``, so updates are explicit DELETE + INSERT.
    """
    # Dedupe by session id, last one wins (real batches contain duplicates —
    # the sessions table uses INSERT OR REPLACE for the same reason).
    deduped: dict[str, Tuple[Optional[str], Optional[int]]] = {
        sid: (text, mtime) for sid, text, mtime in items
    }
    if not deduped:
        return 0
    if not embeddings_available() or not ensure_session_vec_table(conn):
        return 0
    if budget_seconds is None:
        budget_seconds = _embed_budget_seconds()

    try:
        meta = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT session_id, source_mtime_ns, model FROM session_embedding_meta"
            )
        }
        stored = {row[0] for row in conn.execute("SELECT session_id FROM session_embeddings")}
    except sqlite3.Error as exc:
        logger.warning("Session embedding pass failed, leaving FTS-only: %s", exc)
        return 0

    vanished = (meta.keys() | stored) - deduped.keys()
    to_embed = [
        (sid, text)
        for sid, (text, mtime) in deduped.items()
        if text is not None
        and (sid not in stored or sid not in meta or meta[sid] != (mtime, DEFAULT_MODEL))
    ]

    written = 0
    deadline = time.monotonic() + budget_seconds if budget_seconds else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute("BEGIN")
        for sid in vanished:
            conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM session_embedding_meta WHERE session_id = ?", (sid,))

        skipped = 0
        for idx, (sid, text) in enumerate(to_embed):
            if deadline and time.monotonic() > deadline:
                skipped = len(to_embed) - idx
                break
            vector = embed_text((text or "")[:_MAX_EMBED_CHARS])
            if vector is None:
                continue  # empty text or a transient embed failure — FTS covers it
            # vec0 has no working OR REPLACE — updates are DELETE + INSERT.
            conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (sid,))
            conn.execute(
                "INSERT INTO session_embeddings(session_id, model, embedding) VALUES (?, ?, ?)",
                (sid, DEFAULT_MODEL, pack_vector(vector)),
            )
            mtime = deduped[sid][1]
            conn.execute(
                "INSERT OR REPLACE INTO session_embedding_meta"
                "(session_id, source_mtime_ns, model, created) VALUES (?, ?, ?, ?)",
                (sid, mtime, DEFAULT_MODEL, now),
            )
            written += 1
        # Commit whatever we managed — progress survives a budget stop.
        conn.execute("COMMIT")
        if skipped:
            logger.info(
                "Embedding budget (%.0fs) reached: %d written, %d deferred to the next pass",
                budget_seconds or 0,
                written,
                skipped,
            )
    except sqlite3.Error as exc:
        logger.warning("Session embedding pass failed, leaving FTS-only: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        return 0
    return written
