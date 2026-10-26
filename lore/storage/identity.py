"""Namespaced session identity and divergent copies (#155).

A provider session id is only unique inside the provider's own namespace. The
same id can arrive from two tools, two machines or two divergent copies of one
conversation. The store keeps the unqualified id as the primary key (so every
existing tag, memory, export and bookmark keeps resolving) and adds the missing
qualifiers as columns, plus rules for what happens on a collision.

Contract: ``docs/superpowers/specs/2026-10-26-source-revision-and-identity-design.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from ..core.models import UnifiedMessage, UnifiedSession

#: Overrides the machine/home scope in a source namespace.
NAMESPACE_ENV = "LORE_SOURCE_NAMESPACE"

#: Separator between the qualifiers of a copy id: ``<tool>:<ns>:<id>#<n>``.
COPY_SEPARATOR = "#"


def source_namespace_for(session: UnifiedSession) -> str:
    """``<tool>:<machine-or-home-scope>`` for one session.

    The scope defaults to ``local`` and is overridable per host, so two machines
    that share an archive cannot claim the same namespace.
    """
    scope = os.environ.get(NAMESPACE_ENV, "").strip() or "local"
    return f"{session.tool.value}:{scope}"


def _message_fingerprint(message: UnifiedMessage) -> dict:
    """The fields that make a message *content* — not its storage rowid."""
    role = getattr(message.role, "value", str(message.role))
    tool_names = []
    for call in message.tool_calls or []:
        if isinstance(call, dict):
            tool_names.append(str(call.get("tool") or call.get("name") or ""))
    return {
        "role": role,
        "kind": message.kind,
        "content": message.content or "",
        "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        "model": message.model,
        "message_id": message.message_id,
        "tools": tool_names,
    }


def canonical_content_hash(session: UnifiedSession) -> str:
    """Stable hash of a session's meaningful content (used for copy detection).

    Two byte-identical copies hash equal; a divergent history (different
    message order, text, role or model) hashes differently. Storage details
    such as the export path or the parser version are deliberately excluded.
    """
    payload = {
        "tool": session.tool.value,
        "project": session.project_path,
        "thread": session.thread_id,
        "created": session.created_at.isoformat() if session.created_at else None,
        "updated": session.last_updated.isoformat() if session.last_updated else None,
        "messages": [_message_fingerprint(message) for message in session.messages],
    }
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode("utf-8", "surrogatepass")).hexdigest()


def qualified_copy_id(session: UnifiedSession, index: int) -> str:
    """Id for the ``index``-th divergent copy of ``session``."""
    return (
        f"{session.tool.value}:{source_namespace_for(session).split(':', 1)[-1]}:"
        f"{session.session_id}{COPY_SEPARATOR}{index}"
    )


def _existing_row(conn, session_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, source_namespace, source_record_id, content_sha256, copy_of_session_id "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row is not None else None


def resolve_store_id(
    conn,
    session: UnifiedSession,
    *,
    content_hash: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Decide which primary key a session must be written under.

    Returns ``(store_id, copy_of)``:

    - no row yet, or the stored copy has identical content → the unqualified id;
    - a stored row with different content → the qualified copy id and the id of
      the row it is a divergent copy of. The earlier row is never replaced.

    Rule 3 of the spec (different namespace, same record id, unqualified id
    taken) is handled by the same branch: the content differs, so the newcomer
    is the copy.
    """
    digest = content_hash or canonical_content_hash(session)
    existing = _existing_row(conn, session.session_id)
    if existing is None:
        return session.session_id, None
    if existing["content_sha256"] in (None, digest):
        # No hash recorded (legacy row) or identical content: refresh in place.
        return session.session_id, existing["copy_of_session_id"]
    index = 2
    while True:
        candidate = qualified_copy_id(session, index)
        if _existing_row(conn, candidate) is None:
            return candidate, session.session_id
        index += 1


def store_identity_columns(
    conn,
    store_id: str,
    session: UnifiedSession,
    *,
    content_hash: str,
    copy_of: Optional[str],
    record_id: Optional[str] = None,
) -> None:
    """Persist the identity qualifiers of one session row.

    ``record_id`` is the provider's own id, which differs from the stored id when
    the row is a qualified divergent copy.
    """
    conn.execute(
        "UPDATE sessions SET source_namespace = ?, source_record_id = ?, "
        "content_sha256 = ?, copy_of_session_id = ? WHERE id = ?",
        (
            source_namespace_for(session),
            record_id or session.session_id,
            content_hash,
            copy_of,
            store_id,
        ),
    )


def resolve_session_id(conn, value: str) -> Optional[str]:
    """Resolve a legacy, qualified or record id to a stored session id.

    Compatibility resolver for callers that hold an id from an older archive,
    an export file or a user's paste. Returns ``None`` for unknown values so the
    caller can keep its own 404 behaviour.
    """
    if not value:
        return None
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (value,)).fetchone()
    if row is not None:
        return row["id"]
    # A qualified id looks like ``<tool>:<scope>:<record>#<n>``; try the base id
    # and then the bare provider record id it contains.
    candidates = [value]
    if COPY_SEPARATOR in value:
        base = value.split(COPY_SEPARATOR, 1)[0]
        candidates.extend([base, base.rsplit(":", 1)[-1]])
    for candidate in candidates:
        if not candidate or candidate == value:
            continue
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (candidate,)).fetchone()
        if row is not None:
            return row["id"]
    row = conn.execute(
        "SELECT id FROM sessions WHERE source_record_id = ? ORDER BY copy_of_session_id IS NOT NULL, id",
        (value,),
    ).fetchone()
    if row is not None:
        return row["id"]
    for candidate in candidates[1:]:
        row = conn.execute(
            "SELECT id FROM sessions WHERE source_record_id = ? "
            "ORDER BY copy_of_session_id IS NOT NULL, id",
            (candidate,),
        ).fetchone()
        if row is not None:
            return row["id"]
    row = conn.execute(
        "SELECT id FROM sessions WHERE content_sha256 = ? ORDER BY id", (value,)
    ).fetchone()
    return row["id"] if row is not None else None


def copies_of(conn, session_id: str) -> list[dict]:
    """Divergent copies stored for ``session_id`` (oldest first)."""
    rows = conn.execute(
        "SELECT id, source_namespace, content_sha256, created FROM sessions "
        "WHERE copy_of_session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]
