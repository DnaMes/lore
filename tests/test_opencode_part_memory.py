"""Loading an opencode session must not materialise unused part payloads.

Real opencode tool parts carry large fields the extractor never reads
(``state.metadata``, attachments, diffs). One real session had 3337 parts with
351 MB of raw JSON but only ~2 MB of content that ends up in the
``UnifiedSession``; ``fetchall()`` + full ``json.loads`` for every part pushed a
sync run to a ~1.9 GB transient peak. Parts are now reduced to the fields
``_assemble_message_content_from_parts`` reads, as they stream out of SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import tracemalloc

import pytest

from lore.extractors.opencode import OpenCodeExtractor

JUNK_BYTES = 1_000_000
JUNK_PARTS = 40


@pytest.fixture
def extractor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return OpenCodeExtractor()


def _db(parts: list[tuple[str, str, dict]]) -> sqlite3.Connection:
    """In-memory opencode DB with one user message and the given parts."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    conn.execute(
        "INSERT INTO message VALUES ('m1', 'ses-1', 1, 1, ?)",
        (json.dumps({"role": "assistant", "time": {"created": 1}}),),
    )
    for i, (_, _, payload) in enumerate(parts):
        conn.execute(
            "INSERT INTO part VALUES (?, 'm1', 'ses-1', ?, ?, ?)",
            (f"p{i:04d}", i + 1, i + 1, json.dumps(payload)),
        )
    return conn


def _tool_part(junk: str = "", output: str = "result") -> dict:
    state = {"status": "completed", "input": {"path": "a.py"}, "output": output}
    if junk:
        state["metadata"] = {"preview": junk}
    return {
        "type": "tool",
        "tool": "read",
        "callID": "call-1",
        "state": state,
        "attachments": [{"data": junk}] if junk else [],
    }


def test_unused_part_fields_do_not_change_the_loaded_session(extractor):
    """Same content and tool_calls whether or not parts carry unused payload."""
    plain = _db([("", "", {"type": "text", "text": "hi"}), ("", "", _tool_part())])
    noisy = _db([("", "", {"type": "text", "text": "hi"}), ("", "", _tool_part(junk="x" * 5000))])

    (plain_msg,) = extractor._load_messages_from_sqlite(plain, "ses-1")
    (noisy_msg,) = extractor._load_messages_from_sqlite(noisy, "ses-1")

    assert noisy_msg.content == plain_msg.content
    assert noisy_msg.tool_calls == plain_msg.tool_calls
    assert plain_msg.tool_calls == [
        {
            "id": "call-1",
            "tool": "read",
            "status": "completed",
            "input": {"path": "a.py"},
            "output": "result",
            "truncated": False,
        }
    ]


def test_oversized_output_is_still_clamped_after_slimming(extractor):
    """The pathological-output clamp (#54) keeps working on the reduced part."""
    over = "y" * (extractor.MAX_TOOL_PART_CHARS + 10)
    conn = _db([("", "", _tool_part(output=over))])

    (msg,) = extractor._load_messages_from_sqlite(conn, "ses-1")

    call = msg.tool_calls[0]
    assert call["truncated"] is True
    assert call["output"].endswith(f"(truncated, {len(over)} chars total)")


def test_loading_many_heavy_parts_stays_bounded(extractor):
    """Peak Python memory ≈ one part, not the sum of every part's raw JSON."""
    junk = "z" * JUNK_BYTES
    conn = _db([("", "", _tool_part(junk=junk)) for _ in range(JUNK_PARTS)])

    tracemalloc.start()
    try:
        (msg,) = extractor._load_messages_from_sqlite(conn, "ses-1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(msg.tool_calls) == JUNK_PARTS
    # Raw payload is ~2 MB/part (metadata + attachment) → ~80 MB in total.
    assert peak < 15_000_000, f"peak {peak / 1e6:.0f} MB — unused part fields are being retained"
