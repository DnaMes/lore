"""StreamingV2Writer must not retain full session bodies for the embed pass.

``add_full`` queues ``(id, text, mtime)`` for the post-commit embedding pass and
keeps it until ``finalize``. ``embed_sessions`` only ever embeds the first
``_MAX_EMBED_CHARS`` characters, so queueing the whole body pinned every
session's full text in RAM for the entire sync (181 M chars across ~1000
sessions) for no benefit.
"""

from __future__ import annotations

from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage.session_vectors import _MAX_EMBED_CHARS
from lore.storage.writer import StreamingV2Writer


def _big_session(session_id: str, chars: int) -> UnifiedSession:
    now = datetime(2026, 3, 4, 12, 0, 0)
    # Distinct prefix so a wrong truncation point is visible, not just a wrong length.
    body = "".join(f"{i:06d} " for i in range(chars // 7 + 1))[:chars]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=now)],
        project_path="/proj",
        title=f"Session {session_id}",
    )


def _embed_text(session: UnifiedSession) -> str:
    """What the FTS/embed text was before capping: title, newline, message bodies."""
    return f"{session.title}\n" + "\n".join(m.content for m in session.messages)


def _writer(tmp_path) -> StreamingV2Writer:
    writer = StreamingV2Writer(tmp_path / "index.v2.sqlite", {}, {})
    writer.begin()
    return writer


def test_queued_embed_text_is_capped_to_what_gets_embedded(tmp_path):
    writer = _writer(tmp_path)
    try:
        writer.add_full(_big_session("big", _MAX_EMBED_CHARS * 5))

        ((_, text, _),) = writer._embed_inputs
        assert text is not None
        assert len(text) <= _MAX_EMBED_CHARS
    finally:
        writer.abort()


def test_capping_keeps_the_exact_prefix_that_would_be_embedded(tmp_path):
    """Truncating early must be behaviour-preserving: same prefix as before."""
    uncapped = _big_session("same", _MAX_EMBED_CHARS * 3)
    writer = _writer(tmp_path)
    try:
        writer.add_full(uncapped)

        ((_, text, _),) = writer._embed_inputs
        assert text == _embed_text(uncapped)[:_MAX_EMBED_CHARS]
    finally:
        writer.abort()


def test_short_session_text_is_left_untouched(tmp_path):
    writer = _writer(tmp_path)
    try:
        session = _big_session("short", 100)
        writer.add_full(session)

        ((_, text, _),) = writer._embed_inputs
        assert text == _embed_text(session)
    finally:
        writer.abort()
