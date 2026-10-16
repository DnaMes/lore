"""Tests for the v2 SQLite reader (issue #44, PR 3).

`load_index_v2` reads the v2 store back into the legacy index dict shape;
`load_index()` prefers it over index.json behind the LORE_USE_V2 flag,
falling back to JSON transparently. These tests cover both.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.storage import load_index_v2, v2_is_available


def _session(
    sid: str,
    tool: Tool = Tool.CLAUDE_CODE,
    title: str = "T",
    project: str = "/p",
    n_messages: int = 2,
) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path=project,
        title=title,
        messages=[
            UnifiedMessage(
                role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
                content=f"body {i}",
                timestamp=datetime(2026, 1, 1),
            )
            for i in range(n_messages)
        ],
    )


# ---------------------------------------------------------------------------
# v2_is_available
# ---------------------------------------------------------------------------


def test_v2_unavailable_when_no_db(tmp_path):
    assert v2_is_available(tmp_path) is False


def test_v2_available_after_index_build(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    assert v2_is_available(tmp_path) is True


# ---------------------------------------------------------------------------
# load_index_v2
# ---------------------------------------------------------------------------


def test_load_index_v2_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_index_v2(tmp_path)


def test_load_index_v2_shape(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a", title="Hello")], {})
    idx = load_index_v2(tmp_path)
    assert set(idx.keys()) >= {"version", "stats", "sessions"}
    assert idx["version"] == "2"
    assert len(idx["sessions"]) == 1


def test_load_index_v2_session_fields(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a", n_messages=4)], {})
    session = load_index_v2(tmp_path)["sessions"][0]
    assert session["id"] == "a"
    assert session["tool"] == "claude-code"
    assert session["messages"] == 4
    # 4 messages alternate user/assistant -> 2 user prompts
    assert session["prompts"] == 2


def test_load_index_v2_stats(tmp_path):
    IndexBuilder(tmp_path).build_index(
        [_session("a", tool=Tool.CLAUDE_CODE), _session("b", tool=Tool.CODEX)], {}
    )
    stats = load_index_v2(tmp_path)["stats"]
    assert stats["total_sessions"] == 2
    assert stats["by_tool"] == {"claude-code": 1, "codex": 1}


def test_load_index_v2_sorted_newest_first(tmp_path):
    older = _session("old")
    older.last_updated = datetime(2026, 1, 1)
    newer = _session("new")
    newer.last_updated = datetime(2026, 6, 1)
    IndexBuilder(tmp_path).build_index([older, newer], {})
    ids = [s["id"] for s in load_index_v2(tmp_path)["sessions"]]
    assert ids == ["new", "old"]


def test_load_index_v2_carries_prompt_outline(tmp_path):
    s = _session("a", n_messages=0)
    s.messages = [
        UnifiedMessage(
            role=Role.USER, content="please fix the bug", timestamp=datetime(2026, 1, 1)
        ),
    ]
    IndexBuilder(tmp_path).build_index([s], {})
    session = load_index_v2(tmp_path)["sessions"][0]
    assert "fix the bug" in (session["prompt_outline"] or "")


# ---------------------------------------------------------------------------
# load_index() integration — v2 preferred, JSON fallback
# ---------------------------------------------------------------------------


def test_load_index_uses_v2_when_available(tmp_path, monkeypatch):
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    web_data.clear_index_cache()

    IndexBuilder(tmp_path).build_index([_session("v2only", title="From V2")], {})
    payload = web_data.load_index()
    assert payload.get("version") == "2"
    assert payload["sessions"][0]["id"] == "v2only"


def test_load_index_falls_back_to_json_when_flag_off(tmp_path, monkeypatch):
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()

    IndexBuilder(tmp_path).build_index([_session("j", title="From JSON")], {})
    payload = web_data.load_index()
    # JSON index has no "version": "2" marker.
    assert payload.get("version") != "2"
    assert payload["sessions"][0]["id"] == "j"


def test_load_index_falls_back_when_v2_missing(tmp_path, monkeypatch):
    """v2 enabled but no DB yet — must transparently use index.json."""
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    web_data.clear_index_cache()

    # Write only a JSON index — no v2 db.
    (tmp_path / "index.json").write_text(
        '{"version":"1.0.0","stats":{},"sessions":'
        '[{"id":"jsononly","tool":"cursor","title":"t",'
        '"created":"2026-01-01","updated":"2026-01-01","messages":1}]}',
        encoding="utf-8",
    )
    payload = web_data.load_index()
    assert payload["sessions"][0]["id"] == "jsononly"


def test_v2_and_json_agree_on_session_ids(tmp_path, monkeypatch):
    """The v2 reader and the JSON reader must surface the same sessions."""
    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")

    sessions = [_session("a"), _session("b"), _session("c")]
    IndexBuilder(tmp_path).build_index(sessions, {})

    monkeypatch.setenv("LORE_USE_V2", "1")
    web_data.clear_index_cache()
    v2_ids = sorted(s["id"] for s in web_data.load_index()["sessions"])

    monkeypatch.setenv("LORE_USE_V2", "0")
    web_data.clear_index_cache()
    json_ids = sorted(s["id"] for s in web_data.load_index()["sessions"])

    assert v2_ids == json_ids == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# #36 — staleness check + generated_at
# ---------------------------------------------------------------------------


def test_load_index_v2_sets_generated_at(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    idx = load_index_v2(tmp_path)
    assert idx.get("generated_at") is not None


def test_v2_fresh_store_is_available(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    assert v2_is_available(tmp_path, compare_to=tmp_path / "index.json") is True


def test_v2_stale_store_is_rejected(tmp_path):
    """A v2 store older than index.json must be reported unavailable."""
    import time

    IndexBuilder(tmp_path).build_index([_session("a")], {})
    time.sleep(3.2)
    (tmp_path / "index.json").touch()  # index.json now newer than v2
    assert v2_is_available(tmp_path, compare_to=tmp_path / "index.json") is False


def test_v2_available_without_compare_to(tmp_path):
    """Without a compare_to path, no staleness check is applied."""
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    assert v2_is_available(tmp_path) is True


def test_load_index_falls_back_when_v2_stale(tmp_path, monkeypatch):
    """load_index() must serve JSON when the v2 store is stale."""
    import time

    from lore.interfaces import web_data

    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    web_data.clear_index_cache()

    IndexBuilder(tmp_path).build_index([_session("v2sess", title="From V2 store")], {})
    time.sleep(3.2)
    # Rewrite index.json newer, with a different session id.
    (tmp_path / "index.json").write_text(
        '{"version":"1.0.0","stats":{},"sessions":'
        '[{"id":"jsonsess","tool":"cursor","title":"t",'
        '"created":"2026-01-01","updated":"2026-01-01","messages":1}]}',
        encoding="utf-8",
    )
    web_data.clear_index_cache()
    payload = web_data.load_index()
    # v2 is stale -> JSON path wins
    assert payload["sessions"][0]["id"] == "jsonsess"


# ---------------------------------------------------------------------------
# load_session_messages_v2 (#62 — per-message tokens/model for the served path)
# ---------------------------------------------------------------------------


def test_load_session_messages_v2_roundtrips_tokens_and_model(tmp_path):
    from lore.storage import load_session_messages_v2

    session = UnifiedSession(
        tool=Tool.OPENCODE,
        session_id="s-msgs",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="T",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="q",
                timestamp=datetime(2026, 1, 1, 10, 0),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="a",
                timestamp=datetime(2026, 1, 1, 10, 1),
                model="claude-opus-4-8",
                tokens={"input": 100, "output": 20, "total": 120},
            ),
        ],
    )
    IndexBuilder(tmp_path).build_index([session], {})

    loaded = load_session_messages_v2(tmp_path, "s-msgs")
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0].tokens is None
    assert loaded[1].tokens == {"input": 100, "output": 20, "total": 120}
    assert loaded[1].model == "claude-opus-4-8"
    assert loaded[1].role == Role.ASSISTANT


def test_load_session_messages_v2_none_for_missing_session(tmp_path):
    from lore.storage import load_session_messages_v2

    IndexBuilder(tmp_path).build_index([_session("present")], {})
    assert load_session_messages_v2(tmp_path, "absent") is None


def test_load_session_messages_v2_none_when_no_db(tmp_path):
    from lore.storage import load_session_messages_v2

    assert load_session_messages_v2(tmp_path, "anything") is None


# ---------------------------------------------------------------------------
# served-path token backfill (#62)
# ---------------------------------------------------------------------------


def test_backfill_v2_message_tokens_fills_matching_count(monkeypatch, tmp_path):
    from lore.interfaces import web

    served = UnifiedSession(
        tool=Tool.OPENCODE,
        session_id="s1",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        messages=[
            UnifiedMessage(Role.USER, "q", datetime(2026, 1, 1)),
            UnifiedMessage(Role.ASSISTANT, "a", datetime(2026, 1, 1)),
        ],
    )
    v2_msgs = [
        UnifiedMessage(Role.USER, "q", datetime(2026, 1, 1)),
        UnifiedMessage(Role.ASSISTANT, "a", datetime(2026, 1, 1), model="m1", tokens={"total": 99}),
    ]
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("lore.storage.load_session_messages_v2", lambda _dir, _sid: v2_msgs)

    web._backfill_v2_message_tokens("s1", served)

    assert served.messages[1].tokens == {"total": 99}
    assert served.messages[1].model == "m1"


def test_backfill_v2_message_tokens_skips_on_count_mismatch(monkeypatch, tmp_path):
    from lore.interfaces import web

    served = UnifiedSession(
        tool=Tool.OPENCODE,
        session_id="s1",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        messages=[UnifiedMessage(Role.USER, "q", datetime(2026, 1, 1))],
    )
    v2_msgs = [
        UnifiedMessage(Role.USER, "q", datetime(2026, 1, 1)),
        UnifiedMessage(Role.ASSISTANT, "a", datetime(2026, 1, 1), tokens={"total": 99}),
    ]
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("lore.storage.load_session_messages_v2", lambda _dir, _sid: v2_msgs)

    web._backfill_v2_message_tokens("s1", served)

    # Count mismatch → no misaligned backfill.
    assert served.messages[0].tokens is None
