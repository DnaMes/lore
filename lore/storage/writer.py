"""Write UnifiedSession objects into the v2 SQLite store (issue #44, PR 2).

This is the *dual-write* stage: ``IndexBuilder`` keeps producing the legacy
``index.json`` + ``index.sqlite`` exactly as before and additionally calls
:func:`write_sessions` here to mirror the same data into the v2 schema
(``index_v2.sqlite``). Nothing reads v2 yet — PR 3 flips the readers over.

Writes are transaction-scoped upserts: sessions not mentioned by a build remain
available, while a refreshed session replaces only its own messages and search
row. Callers treat failures here as non-fatal — a v2 write error must never
break the legacy index path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.models import MESSAGE_KINDS, ORIGINS, RELATIONS, UnifiedMessage, UnifiedSession
from .envelope import ENVELOPE_VERSION, envelope_for
from .identity import canonical_content_hash, resolve_store_id, store_identity_columns
from .schema import initialise
from .session_vectors import _MAX_EMBED_CHARS, embed_sessions
from .source_revisions import capture_session_source

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


_MAX_TOOL_EVENTS = 50
_MAX_TOOL_NAME_CHARS = 80
_MAX_REASONING_CHARS = 20_000
# Total reasoning kept per session; later messages get it truncated or dropped.
_MAX_SESSION_REASONING_CHARS = 200_000
_ERROR_STATUSES = ("error", "failed", "failure")


def _scrub(text: str) -> str:
    """Replace lone surrogates (JS-truncated emoji) that SQLite cannot encode."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _tool_events_json(message: UnifiedMessage) -> Optional[str]:
    """Compact per-call summary: no arguments and no results are stored.

    ``tool_events`` (already compact) wins; otherwise the structured
    ``tool_calls`` are reduced.
    """
    source = message.tool_events or message.tool_calls or []
    events = []
    for call in source[:_MAX_TOOL_EVENTS]:
        if not isinstance(call, dict):
            continue
        name = _scrub(str(call.get("tool") or call.get("name") or "tool"))
        status = call.get("status")
        duration = call.get("duration_ms")
        events.append(
            {
                "name": name[:_MAX_TOOL_NAME_CHARS],
                "status": _scrub(str(status)) if status is not None else None,
                "duration_ms": duration if isinstance(duration, (int, float)) else None,
                "is_error": bool(call.get("is_error"))
                or str(status or "").lower() in _ERROR_STATUSES,
            }
        )
    if len(source) > _MAX_TOOL_EVENTS:
        events.append(
            {
                "name": "…",
                "status": "truncated",
                "duration_ms": None,
                "is_error": False,
                "dropped": len(source) - _MAX_TOOL_EVENTS,
            }
        )
    return json.dumps(events) if events else None


def _contract_values(
    origin: object, parent: object, relation: object, source_version: object, parser: object
) -> Tuple[str, Optional[str], Optional[str], Optional[str], int]:
    """Normalise the migration-17 session values (unknown enums become defaults)."""
    try:
        parser_version = int(parser or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        parser_version = 0
    return (
        origin if isinstance(origin, str) and origin in ORIGINS else "unknown",
        str(parent) if parent else None,
        relation if isinstance(relation, str) and relation in RELATIONS else None,
        str(source_version) if source_version else None,
        parser_version,
    )


_CONTRACT_DEFAULTS: Tuple[Any, ...] = ("unknown", None, None, None, 0)


def _store_contract_columns(
    conn: sqlite3.Connection, session_id: str, values: Tuple[Any, ...]
) -> None:
    conn.execute(
        "UPDATE sessions SET origin = ?, parent_session_id = ?, relation = ?, "
        "source_version = ?, parser_version = ? WHERE id = ?",
        (*values, session_id),
    )


def _store_entry_contract_columns(conn: sqlite3.Connection, entry: Dict) -> None:
    """Persist an index entry's contract fields without downgrading a stored row.

    An entry from an older index carries defaults (version 0): it must not wipe
    values a newer parse stored, so the update applies only while the stored
    ``parser_version`` is not above the entry's.
    """
    values = _contract_values(
        entry.get("origin"),
        entry.get("parent_session_id"),
        entry.get("relation"),
        entry.get("source_version"),
        entry.get("parser_version"),
    )
    if values == _CONTRACT_DEFAULTS:
        return
    conn.execute(
        "UPDATE sessions SET origin = ?, parent_session_id = ?, relation = ?, "
        "source_version = ?, parser_version = ? WHERE id = ? AND parser_version <= ?",
        (*values, entry["id"], values[4]),
    )


def _write_contract_columns(conn: sqlite3.Connection, session: UnifiedSession) -> None:
    """Persist the migration-17 session columns of a freshly extracted session."""
    values = _contract_values(
        session.origin,
        session.parent_session_id,
        session.relation,
        session.source_version,
        session.parser_version,
    )
    _store_contract_columns(conn, session.session_id, values)


def _source_mtime_ns(source_path: Optional[str], override: Optional[int] = None) -> Optional[int]:
    if override is not None:
        return override
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
# ON CONFLICT keeps the existing sessions row in place. INSERT OR REPLACE would
# delete that row first, triggering ON DELETE CASCADE and destroying messages.
# Last write wins for duplicate ids; extractors deduplicate ahead of us, this is
# a safety net for duplicate session ids from upstream.
_SESSION_INSERT = """
    INSERT INTO sessions (
        id, tool, project, thread_id, title, created, updated,
        source_path, source_mtime_ns, git_branch, git_commit,
        cli_version, metadata_json,
        messages_count, prompt_count, prompt_outline, export_path,
        total_tokens, messages_synced
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        tool = excluded.tool,
        project = excluded.project,
        thread_id = excluded.thread_id,
        title = excluded.title,
        created = excluded.created,
        updated = excluded.updated,
        source_path = excluded.source_path,
        source_mtime_ns = excluded.source_mtime_ns,
        git_branch = excluded.git_branch,
        git_commit = excluded.git_commit,
        cli_version = excluded.cli_version,
        metadata_json = excluded.metadata_json,
        messages_count = excluded.messages_count,
        prompt_count = excluded.prompt_count,
        prompt_outline = excluded.prompt_outline,
        export_path = excluded.export_path,
        total_tokens = excluded.total_tokens,
        messages_synced = excluded.messages_synced
"""


def _session_fts_rowids(conn: sqlite3.Connection) -> Dict[str, List[int]]:
    """Map every session id to its FTS rowids with one scan of ``search_index``.

    ``entity_id`` is UNINDEXED, so each ``DELETE ... WHERE entity_id = ?`` reads
    the whole FTS table (~0.3 s at 6000 sessions). A writer that rewrites many
    sessions in one transaction resolves rowids here once and deletes by rowid.
    """
    rows: Dict[str, List[int]] = {}
    for rowid, entity_id in conn.execute(
        "SELECT rowid, entity_id FROM search_index WHERE entity_type = 'session'"
    ):
        rows.setdefault(entity_id, []).append(rowid)
    return rows


def _replace_session_fts(
    conn: sqlite3.Connection,
    session_id: str,
    tool: str,
    project: Optional[str],
    title: str,
    body: str,
    fts_rows: Optional[Dict[str, List[int]]] = None,
) -> None:
    """Replace only one session's FTS row inside the active transaction.

    ``fts_rows`` is the writer's :func:`_session_fts_rowids` map, kept current
    here; without it the old row is found by a full scan of the FTS table.
    """
    if fts_rows is None:
        conn.execute(
            "DELETE FROM search_index WHERE entity_type = 'session' AND entity_id = ?",
            (session_id,),
        )
    else:
        for rowid in fts_rows.pop(session_id, ()):
            conn.execute("DELETE FROM search_index WHERE rowid = ?", (rowid,))
    cursor = conn.execute(
        """
        INSERT INTO search_index (
            entity_type, entity_id, tool, project, title, body
        ) VALUES ('session', ?, ?, ?, ?, ?)
        """,
        (session_id, tool, project or "", title, body),
    )
    if fts_rows is not None:
        fts_rows[session_id] = [int(cursor.lastrowid or 0)]


def _rebuild_session_fts(
    conn: sqlite3.Connection,
    session_id: str,
    tool: str,
    project: Optional[str],
    title: str,
    fts_rows: Optional[Dict[str, List[int]]] = None,
) -> None:
    """Rebuild one session's FTS row as title + its stored message bodies."""
    body_parts = [title]
    body_parts.extend(
        content
        for (content,) in conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        )
        if content
    )
    _replace_session_fts(
        conn, session_id, tool, project, title, "\n".join(p for p in body_parts if p), fts_rows
    )


def _refresh_complete_session(
    conn: sqlite3.Connection,
    entry: Dict,
    title: str,
    previous_title: str,
    fts_rows: Optional[Dict[str, List[int]]] = None,
) -> None:
    """Refresh metadata of a session whose message rows are already complete.

    Only the columns the index dict actually carries are updated, so
    ``cli_version``, ``metadata_json`` and ``messages_synced`` survive. The FTS
    row holds title + message bodies and is left alone unless the title moved:
    replacing it with the dict's short ``search_text`` would drop the message
    text, and ``entity_id`` is UNINDEXED so each replace scans the whole table.
    """
    session_id = entry["id"]
    conn.execute(
        """
        UPDATE sessions SET
            tool = ?, project = ?, thread_id = ?, title = ?, created = ?, updated = ?,
            source_path = ?, source_mtime_ns = ?, git_branch = ?, git_commit = ?,
            messages_count = ?, prompt_count = ?, prompt_outline = ?, export_path = ?,
            total_tokens = ?
        WHERE id = ?
        """,
        (
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
            int(entry.get("messages") or 0),
            int(entry.get("prompts") or 0),
            entry.get("prompt_outline"),
            entry.get("export_path"),
            int(entry.get("tokens") or 0),
            session_id,
        ),
    )
    # Heals a row still at the migration defaults when the entry carries a version.
    _store_entry_contract_columns(conn, entry)
    if title != previous_title:
        _rebuild_session_fts(
            conn, session_id, entry.get("tool") or "", entry.get("project"), title, fts_rows
        )


def _write_reused_entry(
    conn: sqlite3.Connection, entry: Dict, fts_rows: Optional[Dict[str, List[int]]] = None
) -> None:
    """Write a metadata-only row from a pre-built index dict.

    Incremental sync hands the IndexBuilder pre-built dicts for unchanged
    sessions instead of re-extracting them — so we have no UnifiedMessage
    objects for those. A new row is written with ``messages_synced = 0`` so
    readers and the backfill (#35) can tell it apart from a fully-synced
    session. If the row already has complete messages, the metadata update
    preserves them.
    """
    session_id = entry.get("id")
    if not session_id:
        return
    title = entry.get("title") or ""
    existing = conn.execute(
        "SELECT messages_synced, title FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if existing and existing[0]:
        _refresh_complete_session(
            conn, entry, title, previous_title=existing[1] or "", fts_rows=fts_rows
        )
        return
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
            int(existing[0]) if existing else 0,
        ),
    )
    _store_entry_contract_columns(conn, entry)
    _replace_session_fts(
        conn,
        session_id,
        entry.get("tool") or "",
        entry.get("project"),
        title,
        entry.get("search_text") or "",
        fts_rows,
    )


def _write_full_session(
    conn: sqlite3.Connection,
    session: UnifiedSession,
    title: str,
    session_extras: Dict,
    fts_rows: Optional[Dict[str, List[int]]] = None,
) -> Tuple[str, Optional[str], Optional[int]]:
    """Write one full session (row + message rows + FTS) into the v2 store.

    Returns the ``(session_id, fts_body, source_mtime_ns)`` embed-input triple
    for the post-commit vector pass. Shared by :func:`write_sessions` (list
    path) and :class:`StreamingV2Writer` (streaming path) so both produce
    identical rows.

    Divergent copies (#155): when the id already exists with *different* content,
    this row is written under a qualified id and linked to the earlier one. The
    existing row is never replaced — a second provider history under the same
    unqualified id used to be destroyed by last-write-wins.
    """
    record_id = session.session_id
    content_hash = canonical_content_hash(session)
    store_id, copy_of = resolve_store_id(conn, session, content_hash=content_hash)
    if store_id != session.session_id:
        logger.info(
            "Divergent copy of %s stored as %s (copy_of=%s)",
            session.session_id,
            store_id,
            copy_of,
        )
        session = replace(session, session_id=store_id)
    source_mtime_ns = _source_mtime_ns(session.source_path, session.source_mtime_ns)
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session.session_id,))
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

    _write_contract_columns(conn, session)

    body_parts = [title]
    reasoning_budget = _MAX_SESSION_REASONING_CHARS
    for seq, message in enumerate(session.messages):
        content = _scrub(message.content or "")
        reasoning = _scrub(message.reasoning or "")[: min(_MAX_REASONING_CHARS, reasoning_budget)]
        reasoning_budget -= len(reasoning)
        role = getattr(message.role, "value", str(message.role))
        envelope = envelope_for(message)
        conn.execute(
            """
            INSERT INTO messages (
                session_id, seq, role, content, timestamp, model, tokens_json,
                kind, parent_id, active, tool_events_json, reasoning,
                native_id, content_blocks_json, tool_calls_json, status,
                extensions_json, envelope_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                seq,
                role,
                content,
                message.timestamp.isoformat() if message.timestamp else None,
                message.model,
                json.dumps(message.tokens) if message.tokens else None,
                message.kind if message.kind in MESSAGE_KINDS else "text",
                message.parent_id or None,
                1 if message.active else 0,
                _tool_events_json(message),
                reasoning or None,
                envelope["native_id"],
                envelope["content_blocks_json"],
                envelope["tool_calls_json"],
                envelope["status"],
                envelope["extensions_json"],
                ENVELOPE_VERSION,
            ),
        )
        body_parts.append(content)

    # One FTS row per session: title + concatenated message bodies.
    fts_body = "\n".join(p for p in body_parts if p)
    _replace_session_fts(
        conn,
        session.session_id,
        session.tool.value,
        session.project_path,
        title,
        fts_body,
        fts_rows,
    )
    _link_source_revision(conn, session, source_mtime_ns)
    store_identity_columns(
        conn,
        session.session_id,
        session,
        content_hash=content_hash,
        copy_of=copy_of,
        record_id=record_id,
    )
    return (session.session_id, fts_body, source_mtime_ns)


def _link_source_revision(
    conn: sqlite3.Connection,
    session: UnifiedSession,
    source_mtime_ns: Optional[int],
) -> None:
    """Capture the session's source file as an immutable revision (#151).

    Best effort by design: a failed capture must never fail a sync. The revision
    id lands on the session row so readers can show the raw evidence behind a
    parsed session (and re-parse it later with a fixed parser).
    """
    try:
        result = capture_session_source(
            conn,
            tool=session.tool.value,
            source_path=session.source_path,
            parsed_mtime_ns=source_mtime_ns,
        )
    except Exception as exc:  # noqa: BLE001 - capture never breaks a sync
        logger.warning(
            "Source capture skipped for %s: %s", getattr(session, "source_path", None), exc
        )
        return
    if result.revision_id is None:
        return
    conn.execute(
        "UPDATE sessions SET source_revision_id = ? WHERE id = ?",
        (result.revision_id, session.session_id),
    )


def _write_reused_full_session(
    conn: sqlite3.Connection,
    session: UnifiedSession,
    title: str,
    session_extras: Dict,
    fts_rows: Optional[Dict[str, List[int]]] = None,
) -> Tuple[bool, Tuple[str, Optional[str], Optional[int]]]:
    """Update reused metadata without rewriting messages when they are complete."""
    row = conn.execute(
        "SELECT messages_synced, parser_version FROM sessions WHERE id = ?", (session.session_id,)
    ).fetchone()
    if not row or not row[0]:
        return False, _write_full_session(conn, session, title, session_extras, fts_rows)
    stored_parser_version = int(row[1] or 0)

    source_mtime_ns = _source_mtime_ns(session.source_path, session.source_mtime_ns)
    values = (
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
        1,
    )
    conn.execute(_SESSION_INSERT, values)
    # Stored origin/parent/relation/source_version/parser_version survive a
    # reuse; only a session parsed by a newer parser than the stored one
    # replaces them (fresh wins on re-extraction).
    if int(session.parser_version or 0) > stored_parser_version:
        _write_contract_columns(conn, session)
    body_parts = [title]
    body_parts.extend(
        content
        for (content,) in conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY seq",
            (session.session_id,),
        )
        if content
    )
    fts_body = "\n".join(body_parts)
    _replace_session_fts(
        conn,
        session.session_id,
        session.tool.value,
        session.project_path,
        title,
        fts_body,
        fts_rows,
    )
    _link_source_revision(conn, session, source_mtime_ns)
    current_copy = conn.execute(
        "SELECT copy_of_session_id FROM sessions WHERE id = ?", (session.session_id,)
    ).fetchone()
    store_identity_columns(
        conn,
        session.session_id,
        session,
        content_hash=canonical_content_hash(session),
        copy_of=current_copy[0] if current_copy else None,
    )
    return True, (session.session_id, None, source_mtime_ns)


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
    differs. The write is one transaction: ``begin()`` opens it, each ``add_*``
    upserts only its session, and :meth:`finalize` stamps + commits + runs the
    post-commit embed pass. Sessions not mentioned by the transaction remain.
    """

    def __init__(self, db_path: Path, titles: Dict[str, str], extras: Dict[str, Dict]):
        self.db_path = db_path
        self.titles = titles
        self.extras = extras
        self.conn = initialise(db_path)
        self.count = 0
        self._embed_inputs: List[Tuple[str, Optional[str], Optional[int]]] = []
        self._begun = False
        # Filled by begin(); None keeps the per-delete scan for a writer never begun.
        self._fts_rows: Optional[Dict[str, List[int]]] = None

    def begin(self) -> None:
        # IMMEDIATE: the rowid scan below is a read, and a deferred transaction that
        # reads first cannot upgrade to a write if another writer committed meanwhile
        # (SQLITE_BUSY ignores busy_timeout there). Taking the lock up front waits instead.
        self.conn.execute("BEGIN IMMEDIATE")
        self._begun = True
        self._fts_rows = _session_fts_rowids(self.conn)

    def add_full(self, session: UnifiedSession) -> None:
        title = self.titles.get(session.session_id) or session.title or ""
        triple = _write_full_session(
            self.conn, session, title, self.extras.get(session.session_id, {}), self._fts_rows
        )
        # Queue only what embed_sessions will actually embed. The full body
        # (all message text) would otherwise stay pinned until finalize().
        session_id, text, mtime = triple
        capped = text[:_MAX_EMBED_CHARS] if text is not None else None
        self._embed_inputs.append((session_id, capped, mtime))
        self.count += 1

    def add_reused_entry(self, entry: Dict) -> None:
        _write_reused_entry(self.conn, entry, self._fts_rows)
        entry_id = entry.get("id")
        if entry_id:
            # text=None: keep the stored vector, never (re)embed a reused row.
            self._embed_inputs.append((str(entry_id), None, None))
        self.count += 1

    def add_reused_full(self, session: UnifiedSession) -> None:
        """Preserve complete reused messages while refreshing session metadata."""
        title = self.titles.get(session.session_id) or session.title or ""
        preserved, triple = _write_reused_full_session(
            self.conn, session, title, self.extras.get(session.session_id, {}), self._fts_rows
        )
        if preserved:
            self._embed_inputs.append((session.session_id, None, None))
        else:
            self._embed_inputs.append(triple)
        self.count += 1

    def finalize(self) -> int:
        # The embedding pass removes ids absent from its input. Keep every
        # session retained by this transaction so archive preservation applies
        # to vectors as well as session/message rows.
        embedded_ids = {session_id for session_id, _, _ in self._embed_inputs}
        for (session_id,) in self.conn.execute("SELECT id FROM sessions"):
            if session_id not in embedded_ids:
                self._embed_inputs.append((session_id, None, None))
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
        except sqlite3.Error:
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
    """Upsert ``sessions`` (+ reused entries) without deleting other rows.

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
    except Exception:  # noqa: BLE001 - abort the transaction and connection, then re-raise
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
