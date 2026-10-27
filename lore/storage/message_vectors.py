"""Populate per-message embedding vectors for message-level retrieval (#190).

One vector per message, stored in the vec0 ``message_embeddings`` table
(created lazily by :func:`lore.storage.embeddings.ensure_message_vec_table`)
and keyed by the stable ``<session_id>#m<seq>`` identity the message permalink
uses — not the ``messages`` rowid, which a reindex renumbers.
``message_embedding_meta`` (migration 25, content hash in migration 28) records
which model produced each vector and a sha256 of the embedded text, so the pass
is incremental by ``(session_id, seq, model, content)``: a message that already
has a current-model vector for its current content is skipped. The writer can
replace message bodies; without the hash, such a message kept scoring against
its stale vector.

The pass is **store-driven**: it selects the messages that still lack a
current-model vector instead of taking a caller-supplied list. A run stopped by
the time budget therefore resumes where it stopped — the next call just re-runs
the same query. :func:`lore.storage.message_search.search_messages` uses the
result through the KNN path; without it, message retrieval stays on FTS.

Everything here is **best-effort**, like the session pass (#95): when
sqlite-vec or fastembed is missing, or a single embed fails, the affected
messages simply get no vector. Callers must run this outside a write
transaction — embedding is slow and would hold the write lock.

Deleting a message (or a session) leaves its vector row behind on purpose:
retrieval joins back to the live ``messages`` row on ``(session_id, seq)``, so
an orphan can never surface stale content, and vec0 has no efficient
``NOT IN (subquery)`` delete.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .embeddings import (
    DEFAULT_MODEL,
    embed_text,
    embeddings_available,
    ensure_message_vec_table,
    pack_vector,
)
from .session_vectors import _MAX_EMBED_CHARS, _embed_budget_seconds

logger = logging.getLogger(__name__)

# One pending message: (session_id, seq, content to embed).
PendingMessage = Tuple[str, int, str]


def _content_hash(content: str) -> str:
    """sha256 of the text a vector is computed from (hex), or '' for empty.

    Hashes the same truncation ``embed_messages`` embeds, so the stored hash
    always describes the text behind the stored vector.
    """
    return hashlib.sha256(content[:_MAX_EMBED_CHARS].encode("utf-8")).hexdigest()


def message_key(session_id: str, seq: int) -> str:
    """Stable vector-store identity of one message (same identity as the permalink)."""
    return f"{session_id}#m{int(seq)}"


def pending_messages(conn: sqlite3.Connection) -> List[PendingMessage]:
    """Messages that have no current vector for their content, in session/seq order.

    The ``LEFT JOIN`` is on ``(session_id, seq)`` — the columns
    ``message_embedding_meta`` indexes — so a message is re-embedded when its
    stored vector came from another model, or when its stored ``content_hash``
    is missing (pre-migration-28 rows, re-embedded exactly once) or differs
    from the hash of the current body (the writer replaced the text). Empty and
    whitespace-only bodies are skipped: they carry nothing to search. The hash
    comparison runs in Python — SQLite has no built-in sha256.
    """
    rows = conn.execute(
        """
        SELECT m.session_id, m.seq, m.content,
               meta.model, meta.content_hash
        FROM messages m
        LEFT JOIN message_embedding_meta meta
               ON meta.session_id = m.session_id AND meta.seq = m.seq
        WHERE TRIM(COALESCE(m.content, '')) <> ''
        ORDER BY m.session_id, m.seq
        """
    ).fetchall()
    pending = []
    for row in rows:
        content = str(row[2] or "")
        stored_hash = row[4]
        current_hash = _content_hash(content)
        # A row is current only when it exists, came from the current model,
        # and its hash matches the current body. NULL hash (pre-migration-28
        # legacy rows) never matches — such a row is re-embedded exactly once,
        # after which a real hash is stored.
        if row[3] == DEFAULT_MODEL and stored_hash == current_hash:
            continue
        pending.append((str(row[0]), int(row[1]), content))
    return pending


def embed_messages(
    conn: sqlite3.Connection,
    *,
    budget_seconds: Optional[float] = None,
) -> int:
    """Embed pending message bodies into ``message_embeddings``.

    Bounded by ``budget_seconds`` (default from ``LORE_EMBED_BUDGET_SECONDS``,
    ``0`` = unlimited — the one-shot ``lore embed-messages`` backfill). Each
    body is capped at ``_MAX_EMBED_CHARS`` like the session pass, so one
    pathological message cannot spend seconds tokenising text the model
    discards anyway. Progress is committed before the budget check stops the
    pass, so a stopped run is resumed by calling this again.

    Returns the number of vectors written. Returns 0 — without raising — when
    the vector table or the embedding backend is unavailable.
    """
    if not embeddings_available() or not ensure_message_vec_table(conn):
        return 0
    if budget_seconds is None:
        budget_seconds = _embed_budget_seconds()

    try:
        pending = pending_messages(conn)
    except sqlite3.Error as exc:
        logger.warning("Message embedding pass failed, leaving FTS-only: %s", exc)
        return 0
    if not pending:
        return 0

    written = 0
    skipped = 0
    deadline = time.monotonic() + budget_seconds if budget_seconds else None
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute("BEGIN")
        for idx, (session_id, seq, content) in enumerate(pending):
            if deadline and time.monotonic() > deadline:
                skipped = len(pending) - idx
                break
            vector = embed_text(content[:_MAX_EMBED_CHARS])
            if vector is None:
                continue  # an embed failure must not abort the pass
            key = message_key(session_id, seq)
            # vec0 has no working OR REPLACE — an update is DELETE + INSERT.
            conn.execute("DELETE FROM message_embeddings WHERE message_key = ?", (key,))
            conn.execute(
                "INSERT INTO message_embeddings(message_key, model, embedding) VALUES (?, ?, ?)",
                (key, DEFAULT_MODEL, pack_vector(vector)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO message_embedding_meta"
                "(message_key, session_id, seq, model, content_hash, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    session_id,
                    seq,
                    DEFAULT_MODEL,
                    _content_hash(content),
                    created,
                ),
            )
            written += 1
        conn.execute("COMMIT")
        if skipped:
            logger.info(
                "Message embedding budget (%.0fs) reached: %d written, %d deferred to the next run",
                budget_seconds or 0,
                written,
                skipped,
            )
    except sqlite3.Error as exc:
        logger.warning("Message embedding pass failed, leaving FTS-only: %s", exc)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        return 0
    return written
