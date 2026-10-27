"""Token aggregation for the cost dashboard (#67).

Covers the three layers that were broken: the per-message token math, the Claude
usage parser, and the writer→reader roundtrip that carries total_tokens through
the v2 store into the index dict the cost endpoint reads.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession, _message_token_total
from lore.extractors.claude import _parse_usage_tokens
from lore.storage import load_index_v2, v2_db_path, write_sessions


def test_message_token_total_handles_each_format():
    # Explicit total wins.
    assert _message_token_total({"total": 500, "input": 1, "output": 1}) == 500
    # OpenCode raw payload: input/output, no total.
    assert _message_token_total({"input": 100, "output": 23}) == 123
    # Claude-style keys.
    assert _message_token_total({"input_tokens": 50, "output_tokens": 7}) == 57
    # Junk / missing.
    assert _message_token_total(None) == 0
    assert _message_token_total({}) == 0
    assert _message_token_total({"cache": {"read": 9}}) == 0


def test_session_total_tokens_sums_mixed_messages():
    session = UnifiedSession(
        tool=Tool.OPENCODE,
        session_id="s-tok",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        messages=[
            UnifiedMessage(
                Role.ASSISTANT, "a", datetime(2026, 1, 1), tokens={"input": 100, "output": 20}
            ),
            UnifiedMessage(Role.ASSISTANT, "b", datetime(2026, 1, 1), tokens={"total": 30}),
            UnifiedMessage(Role.USER, "q", datetime(2026, 1, 1), tokens=None),
        ],
    )
    assert session.total_tokens == 150


def test_parse_usage_tokens_from_claude_usage():
    usage = {
        "input_tokens": 50910,
        "output_tokens": 431,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 33087,
    }
    tokens = _parse_usage_tokens(usage)
    assert tokens == {
        "input": 50910,
        "output": 431,
        "total": 51341,
        # Cache accounting is preserved for the canonical envelope (#157); it is
        # not part of the headline total, which stays input+output.
        "cache_creation": 33087,
        "cache_read": 0,
    }

    # No usable counts → None (so total_tokens stays None, not a phantom 0).
    assert _parse_usage_tokens({}) is None
    assert _parse_usage_tokens(None) is None
    assert _parse_usage_tokens({"input_tokens": 0, "output_tokens": 0}) is None


def test_total_tokens_roundtrips_through_v2_store(tmp_path):
    """A session's token total must survive write → read so the cost endpoint sees it."""
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="s-roundtrip",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Roundtrip",
        messages=[
            UnifiedMessage(
                Role.ASSISTANT,
                "answer",
                datetime(2026, 1, 1),
                tokens={"input": 1000, "output": 200, "total": 1200},
            ),
        ],
    )

    db_path = v2_db_path(tmp_path)
    write_sessions(db_path, [session], {"s-roundtrip": "Roundtrip"})

    # Column exists (migration 11 applied) and the value persisted.
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    assert "total_tokens" in cols
    stored = conn.execute(
        "SELECT total_tokens FROM sessions WHERE id = ?", ("s-roundtrip",)
    ).fetchone()[0]
    conn.close()
    assert stored == 1200

    payload = load_index_v2(tmp_path)
    entry = next(s for s in payload["sessions"] if s["id"] == "s-roundtrip")
    assert entry["tokens"] == 1200


def test_normalize_tool_call_maps_all_extractor_formats():
    """#79 — divergent extractor tool_call keys map to one canonical shape."""
    from lore.core.models import normalize_tool_call

    # opencode: already "tool" + "input"
    oc = normalize_tool_call({"id": "1", "tool": "read", "input": {"f": "a"}, "output": "ok"})
    assert oc["tool"] == "read" and oc["input"] == {"f": "a"} and oc["output"] == "ok"

    # claude: "name" → tool
    cl = normalize_tool_call({"type": "tool_use", "name": "Read", "input": {"f": "b"}})
    assert cl["tool"] == "Read" and cl["input"] == {"f": "b"}
    assert cl["type"] == "tool_use"  # extra keys preserved

    # codex/copilot: "arguments" → input, "name" → tool
    cx = normalize_tool_call({"id": "2", "name": "shell", "arguments": {"cmd": "ls"}})
    assert cx["tool"] == "shell" and cx["input"] == {"cmd": "ls"}
    assert "arguments" not in cx and "name" not in cx  # aliases consumed

    # garbage in → safe default
    assert normalize_tool_call(None)["tool"] == "tool"
    assert normalize_tool_call({})["tool"] == "tool"

    # truncated coerced to bool
    assert normalize_tool_call({"tool": "x", "truncated": 1})["truncated"] is True
