"""The canonical archived message body an AI task must analyse (#177).

Analysis paths used to receive a session's *metadata* — an index entry whose
message list is empty, or a first-few-messages excerpt — and would summarise
that reconstruction instead of the archived transcript. This module is the one
place that turns a session id into the canonical body: every message row of the
v2 store, in store (``seq``) order, with the full content.

When the body cannot be loaded the caller gets a :class:`HydrationError` that
carries one of the :data:`REASONS` constants, so a task fails explicitly with
the reason instead of analysing nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import UnifiedMessage, UnifiedSession
from ..storage.reader import load_session_messages_v2
from ..storage.schema import open_connection
from ..storage.writer import v2_db_path

# Closed set of reasons a body can be unavailable; sorted for the docs and for
# a stable CLI/report breakdown.
REASON_NO_STORE = "no-v2-store"
REASON_SESSION_MISSING = "session-not-archived"
REASON_NOT_SYNCED = "messages-not-synced"
REASON_EMPTY = "empty-transcript"
REASONS: Tuple[str, ...] = (
    REASON_NO_STORE,
    REASON_SESSION_MISSING,
    REASON_NOT_SYNCED,
    REASON_EMPTY,
)

SOURCE_V2_STORE = "v2-store"


class HydrationError(RuntimeError):
    """A task asked for the archived body and it could not be loaded (#177).

    The ``reason`` attribute is one of :data:`REASONS`; it is what a report or a
    CLI message prints, so "no transcript" is never reported without a cause.
    """

    def __init__(self, session_id: str, reason: str) -> None:
        super().__init__(f"cannot hydrate session {session_id}: {reason}")
        self.session_id = session_id
        self.reason = reason


@dataclass(frozen=True)
class HydratedBody:
    """The archived transcript handed to an AI task, with its provenance.

    ``revision`` is a hash of the ordered bodies, so a task result can say which
    archived revision it read; ``seq_range`` is the span of message rows the
    body covers. Together they are the input metadata a task records.
    """

    session_id: str
    messages: Tuple[UnifiedMessage, ...]
    revision: str
    seq_range: Tuple[int, int]
    source: str = SOURCE_V2_STORE

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def coverage(self) -> Dict[str, Any]:
        """Message coverage of this body: whole archived span, by construction."""
        return {
            "source": self.source,
            "messages": self.message_count,
            "seq_range": [self.seq_range[0], self.seq_range[1]],
            "complete": True,
            "revision": self.revision,
        }


def _body_revision(messages: List[UnifiedMessage]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(f"{message.role.value}\x1f{message.kind}\x1f{message.content}\x00".encode())
    return digest.hexdigest()


def _archive_state(output_dir: Path, session_id: str) -> Tuple[Optional[str], int, int]:
    """``(unavailable reason, first seq, last seq)`` for one session's message rows."""
    db = v2_db_path(output_dir)
    if not db.exists():
        return REASON_NO_STORE, 0, 0
    conn = open_connection(db)
    try:
        row = conn.execute(
            "SELECT messages_synced FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return REASON_SESSION_MISSING, 0, 0
        if not row[0]:
            return REASON_NOT_SYNCED, 0, 0
        bounds = conn.execute(
            "SELECT COUNT(*), MIN(seq), MAX(seq) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    count, first_seq, last_seq = int(bounds[0] or 0), bounds[1], bounds[2]
    if not count:
        return REASON_EMPTY, 0, 0
    return None, int(first_seq), int(last_seq)


def hydrate_body(output_dir: Path, session_id: str) -> HydratedBody:
    """Load the canonical archived body of ``session_id``.

    Raises:
        HydrationError: when the store, the session row, its synced messages or
            any message at all is missing — with the reason on the exception.
    """
    reason, first_seq, last_seq = _archive_state(output_dir, session_id)
    if reason is not None:
        raise HydrationError(session_id, reason)
    messages = load_session_messages_v2(output_dir, session_id) or []
    if not messages:
        raise HydrationError(session_id, REASON_EMPTY)
    return HydratedBody(
        session_id=session_id,
        messages=tuple(messages),
        revision=_body_revision(messages),
        seq_range=(first_seq, last_seq),
    )


def hydrate_session(
    output_dir: Path, session: UnifiedSession
) -> Tuple[UnifiedSession, HydratedBody]:
    """Return ``session`` carrying its archived body, plus that body's metadata.

    The session's own ``messages`` are replaced, not merged: a caller that built
    the session from an index entry (empty list) and one that parsed a lossy
    live excerpt both end up with the same canonical transcript.
    """
    body = hydrate_body(output_dir, session.session_id)
    return replace(session, messages=list(body.messages)), body
