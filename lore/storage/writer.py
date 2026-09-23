"""Write UnifiedSession objects into the v2 SQLite store (issue #44, PR 2).

This is the *dual-write* stage: ``IndexBuilder`` keeps producing the legacy
``index.json`` + ``index.sqlite`` exactly as before and additionally calls
:func:`write_sessions` here to mirror the same data into the v2 schema
(``index_v2.sqlite``). Nothing reads v2 yet — PR 3 flips the readers over.

The write is a full replace inside one transaction: the v2 DB always
reflects the set of sessions handed in, so a rebuild stays consistent.
Callers treat failures here as non-fatal — a v2 write error must never
break the legacy index path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..core.models import UnifiedSession
from .schema import initialise
from .session_vectors import _MAX_EMBED_CHARS, embed_sessions

logger = logging.getLogger(__name__)

# v2 DB lives beside the legacy index.sqlite until PR 3 promotes it.
V2_DB_NAME = "index_v2.sqlite"


def v2_db_path(output_dir: Path) -> Path:
    """Return the canonical v2 SQLite path for an output directory."""
    return output_dir / V2_DB_NAME


def _session_metadata_json(session: UnifiedSession) -> Optional[str]:
    """Serialise the loose extras that don't get their own column."""
    extras = {}
    if session.summary:
        extras["summary"] = session.summary
    if session.todos:
        extras["todos"] = session.todos
    if session.title_source is not None:
        extras["title_source"] = getattr(session.title_source, "value", str(session.title_source))
    return json.dumps(extras, ensure_ascii=False) if extras else None


def _source_mtime_ns(source_path: Optional[str]) -> Optional[int]:
    if not source_path:
        return None
    try:
        import os

        return os.stat(source_path).st_mtime_ns
    except OSError:
        return None


# The full column list for an INSERT into sessions, used by both the
# UnifiedSession path and the reused-dict path. The trailing
# messages_synced flag is 1 when the session's message rows were written,
# 0 for a metadata-only reused row.
#
# INSERT OR REPLACE so a duplicate session_id from upstream (e.g. Claude
# Code occasionally stores the same sessionId under two project dirs after
# a resume) doesn't abort the whole sync transaction. Last write wins; the
# extractors deduplicate ahead of us, this is a safety net.
_SESSION_INSERT = """
    INSERT OR REPLACE INTO sessions (
        id, tool, project, thread_id, title, created, updated,
        source_path, source_mtime_ns, git_branch, git_commit,
        cli_version, metadata_json,
        messages_count, prompt_count, prompt_outline, export_path,
        total_tokens, messages_synced
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _write_reused_entry(conn: sqlite3.Connection, entry: Dict) -> None:
    """Write a metadata-only row from a pre-built index dict.

    Incremental sync hands the IndexBuilder pre-built dicts for unchanged
    sessions instead of re-extracting them — so we have no UnifiedMessage
    objects for those. The row is written with ``messages_synced = 0`` so
    readers and the backfill (#35) can tell it apart from a fully-synced
    session; its messages are filled in on the next full rebuild.
    """
    session_id = entry.get("id")
    if not session_id:
        return
    title = entry.get("title") or ""
    conn.execute(
        _SESSION_INSERT,
        (
            session_id,
            entry.get("tool"),
            entry.get("project"),
            entry.get("thread_id"),
            title,
            entry.get("created"),
            entry.get("updated"),
            entry.get("source_path"),
            entry.get("source_mtime"),
            entry.get("git_branch"),
            entry.get("git_commit"),
            None,
            None,
            int(entry.get("messages") or 0),
            int(entry.get("prompts") or 0),
            entry.get("prompt_outline"),
            entry.get("export_path"),
            int(entry.get("tokens") or 0),
            0,  # messages_synced — metadata-only, no message rows
        ),
    )
    conn.execute(
        """
        INSERT INTO search_index (
            entity_type, entity_id, tool, project, title, body
        ) VALUES ('session', ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            entry.get("tool") or "",
            entry.get("project") or "",
            title,
            entry.get("search_text") or "",
        ),
    )


def _write_full_session(
    conn: sqlite3.Connection,
    session: UnifiedSession,
    title: str,
    session_extras: Dict,
) -> Tuple[str, Optional[str], Optional[int]]:
    """Write one full session (row + message rows + FTS) into the v2 store.

    Returns the ``(session_id, fts_body, source_mtime_ns)`` embed-input triple
    for the post-commit vector pass. Shared by :func:`write_sessions` (list
    path) and :class:`StreamingV2Writer` (streaming path) so both produce
    identical rows.
    """
    source_mtime_ns = _source_mtime_ns(session.source_path)
    conn.execute(
        _SESSION_INSERT,
        (
            session.session_id,
            session.tool.value,
            session.project_path,
            session.thread_id,
            title,
            session.created_at.isoformat(),
            session.last_updated.isoformat(),
            session.source_path,
            source_mtime_ns,
            session.git_branch,
            session.git_commit,
            session.cli_version,
            _session_metadata_json(session),
            session.message_count,
            session.user_prompt_count,
            session_extras.get("prompt_outline"),
            session_extras.get("export_path"),
            session.total_tokens or 0,
            1,  # messages_synced — full session, message rows written
        ),
    )

    body_parts = [title]
    for seq, message in enumerate(session.messages):
        content = message.content or ""
        role = getattr(message.role, "value", str(message.role))
        conn.execute(
            """
            INSERT INTO messages (
                session_id, seq, role, content, timestamp, model, tokens_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                seq,
                role,
                content,
                message.timestamp.isoformat() if message.timestamp else None,
                message.model,
                json.dumps(message.tokens) if message.tokens else None,
            ),
        )
        body_parts.append(content)

    # One FTS row per session: title + concatenated message bodies.
    fts_body = "\n".join(p for p in body_parts if p)
    conn.execute(
        """
        INSERT INTO search_index (
            entity_type, entity_id, tool, project, title, body
        ) VALUES ('session', ?, ?, ?, ?, ?)
        """,
        (
            session.session_id,
            session.tool.value,
            session.project_path or "",
            title,
            fts_body,
        ),
    )
    return (session.session_id, fts_body, source_mtime_ns)


def _stamp_and_commit(conn: sqlite3.Connection) -> None:
    """Stamp the write time (#36) and commit the v2 transaction."""
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES('generated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    conn.execute("COMMIT")


def _run_embed_pass(
    conn: sqlite3.Connection,
    embed_inputs: List[Tuple[str, Optional[str], Optional[int]]],
) -> None:
    """Best-effort post-commit vector pass. Never fatal (vectors are optional)."""
    try:
        embed_sessions(conn, embed_inputs)
    except Exception as exc:  # noqa: BLE001 - vectors are optional, never fatal
        logger.warning("Session vector pass skipped: %s", exc)


class StreamingV2Writer:
    """Stream sessions into the v2 store one at a time (issue #96).

    The list-based :func:`write_sessions` holds every ``UnifiedSession`` in RAM.
    This writer instead accepts sessions one-by-one via :meth:`add_full` /
    :meth:`add_reused_entry`, so the caller (``IndexBuilder``'s ``MultiWriter``)
    can drop each session right after handing it over — bounding peak memory.

    Rows are identical to :func:`write_sessions`; only the memory profile
    differs. The write is still a single full-replace transaction: ``begin()``
    opens it and clears the store, then each ``add_*`` inserts inside it, and
    :meth:`finalize` stamps + commits + runs the post-commit embed pass.
    """

    def __init__(self, db_path: Path, titles: Dict[str, str], extras: Dict[str, Dict]):
        self.db_path = db_path
        self.titles = titles
        self.extras = extras
        self.conn = initialise(db_path)
        self.count = 0
        self._embed_inputs: List[Tuple[str, Optional[str], Optional[int]]] = []
        self._begun = False

    def begin(self) -> None:
        self.conn.execute("BEGIN")
        # Full replace — ON DELETE CASCADE clears dependent messages; the
        # search_index rows are rebuilt as sessions stream in.
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM search_index")
        self._begun = True

    def add_full(self, session: UnifiedSession) -> None:
        title = self.titles.get(session.session_id) or session.title or ""
        triple = _write_full_session(
            self.conn, session, title, self.extras.get(session.session_id, {})
        )
        # Queue only what embed_sessions will actually embed. The full body
        # (all message text) would otherwise stay pinned until finalize().
        session_id, text, mtime = triple
        capped = text[:_MAX_EMBED_CHARS] if text is not None else None
        self._embed_inputs.append((session_id, capped, mtime))
        self.count += 1

    def add_reused_entry(self, entry: Dict) -> None:
        _write_reused_entry(self.conn, entry)
        entry_id = entry.get("id")
        if entry_id:
            # text=None: keep the stored vector, never (re)embed a reused row.
            self._embed_inputs.append((str(entry_id), None, None))
        self.count += 1

    def finalize(self) -> int:
        _stamp_and_commit(self.conn)
        # Embed AFTER the commit — holding the write lock across model calls
        # would block web-UI reads, and a vector failure must never roll back
        # the committed index.
        _run_embed_pass(self.conn, self._embed_inputs)
        self.conn.close()
        return self.count

    def abort(self) -> None:
        try:
            if self._begun:
                self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        finally:
            self.conn.close()


def write_sessions(
    db_path: Path,
    sessions: Iterable[UnifiedSession],
    titles: Optional[Dict[str, str]] = None,
    reused_entries: Optional[Iterable[Dict]] = None,
    extras: Optional[Dict[str, Dict]] = None,
) -> int:
    """Replace the v2 store's contents with ``sessions`` (+ reused entries).

    Args:
        db_path: path to the v2 SQLite file (see :func:`v2_db_path`).
        sessions: full sessions to persist; their messages are written too.
        titles: optional ``{session_id: inferred_title}`` overrides — lets the
            caller reuse the title it already computed for the JSON index
            instead of falling back to ``session.title``.
        reused_entries: pre-built index dicts (from incremental sync) for
            unchanged sessions. Written as metadata-only rows so the v2 store
            stays complete; they carry no message rows.
        extras: optional ``{session_id: {prompt_outline, export_path}}`` — the
            IndexBuilder already computes these for the JSON index, so we
            denormalise them onto the v2 sessions row instead of recomputing.

    Returns:
        The total number of session rows written (full + reused).

    Kept for callers that already hold a full list (tests, incremental sync's
    reused-sessions path). The streaming index build uses
    :class:`StreamingV2Writer` directly to avoid materialising a list.
    """
    writer = StreamingV2Writer(db_path, titles or {}, extras or {})
    try:
        writer.begin()
        # reused_entries first, then full sessions — preserves the row order the
        # list path produced (and that the JSON index mirrors).
        for entry in reused_entries or []:
            writer.add_reused_entry(entry)
        for session in sessions:
            writer.add_full(session)
        return writer.finalize()
    except sqlite3.Error:
        writer.abort()
        raise


def write_sessions_safe(
    output_dir: Path,
    sessions: Iterable[UnifiedSession],
    titles: Optional[Dict[str, str]] = None,
    reused_entries: Optional[Iterable[Dict]] = None,
    extras: Optional[Dict[str, Dict]] = None,
) -> int:
    """Best-effort wrapper used by IndexBuilder's dual-write.

    A failure to mirror data into v2 must never break the legacy index, so
    any exception is logged and swallowed. Returns the count written, or 0
    on failure.
    """
    try:
        return write_sessions(
            v2_db_path(output_dir),
            sessions,
            titles=titles,
            reused_entries=reused_entries,
            extras=extras,
        )
    except Exception as exc:  # noqa: BLE001 - intentional: v2 write is best-effort
        logger.warning("v2 dual-write skipped (non-fatal): %s", exc)
        return 0
