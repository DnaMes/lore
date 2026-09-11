"""Tests for lore/exporters/index.py — IndexBuilder and _stat_mtime_ns."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder, _stat_mtime_ns

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "test-session-id-123",
    tool: Tool = Tool.CLAUDE_CODE,
    title: str | None = None,
    messages: list[tuple[str, str]] | None = None,
    project_path: str | None = None,
    source_path: str | None = None,
) -> UnifiedSession:
    now = datetime(2025, 6, 15, 10, 0, 0)
    msgs = []
    for role_str, content in messages or [("user", "Hello world from test")]:
        role = Role.USER if role_str == "user" else Role.ASSISTANT
        msgs.append(UnifiedMessage(role=role, content=content, timestamp=now))
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=msgs,
        project_path=project_path,
        title=title,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# _stat_mtime_ns
# ---------------------------------------------------------------------------


def test_stat_mtime_ns_none():
    assert _stat_mtime_ns(None) is None


def test_stat_mtime_ns_empty_string():
    assert _stat_mtime_ns("") is None


def test_stat_mtime_ns_nonexistent():
    assert _stat_mtime_ns("/definitely/does/not/exist.txt") is None


def test_stat_mtime_ns_existing_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    result = _stat_mtime_ns(str(f))
    assert isinstance(result, int)
    assert result > 0


# ---------------------------------------------------------------------------
# IndexBuilder.build_index — basic smoke tests
# ---------------------------------------------------------------------------


def test_build_index_creates_json(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session()
    builder.build_index([session], {})

    index_path = tmp_path / "index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text())
    assert data["version"] == "1.0.0"
    assert len(data["sessions"]) == 1


def test_build_index_creates_sqlite(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session()
    builder.build_index([session], {})

    assert (tmp_path / "index.sqlite").exists()


def test_build_index_session_entry_fields(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(session_id="abc-123-def", title="My Test Session")
    builder.build_index([session], {})

    data = json.loads((tmp_path / "index.json").read_text())
    entry = data["sessions"][0]
    assert entry["id"] == "abc-123-def"
    assert entry["tool"] == "claude-code"
    assert "title" in entry
    assert "created" in entry
    assert "updated" in entry


def test_build_index_stats_computed(tmp_path):
    builder = IndexBuilder(tmp_path)
    s1 = _make_session("s1", Tool.CLAUDE_CODE, project_path="/proj/a")
    s2 = _make_session("s2", Tool.WARP, project_path="/proj/b")
    builder.build_index([s1, s2], {})

    data = json.loads((tmp_path / "index.json").read_text())
    assert data["stats"]["total_sessions"] == 2
    assert "claude-code" in data["stats"]["by_tool"]
    assert "warp" in data["stats"]["by_tool"]


def test_build_index_keyword_index_populated(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(messages=[("user", "implement python testing framework with pytest")])
    builder.build_index([session], {})

    data = json.loads((tmp_path / "index.json").read_text())
    assert len(data["search_index"]) > 0


def test_build_index_with_reused_entries(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session("new-session")
    reused = [
        {
            "id": "old-session",
            "tool": "warp",
            "project": "/some/project",
            "thread_id": None,
            "title": "Old session title",
            "created": "2025-01-01T00:00:00",
            "updated": "2025-01-01T01:00:00",
            "messages": 5,
            "prompts": 3,
            "prompt_outline": "First message",
            "export_path": None,
            "keywords": ["python", "testing"],
            "search_text": "python testing code",
        }
    ]
    builder.build_index([session], {}, reused_entries=reused)

    data = json.loads((tmp_path / "index.json").read_text())
    ids = [e["id"] for e in data["sessions"]]
    assert "old-session" in ids
    assert "new-session" in ids


def test_build_index_ignored_sessions_excluded(tmp_path):
    # Create an ignored.json file
    ignored = {"session_ids": ["bad-session-id"]}
    (tmp_path / "ignored.json").write_text(json.dumps(ignored))

    builder = IndexBuilder(tmp_path)
    good = _make_session("good-session")
    bad = _make_session("bad-session-id")
    builder.build_index([good, bad], {})

    data = json.loads((tmp_path / "index.json").read_text())
    ids = [e["id"] for e in data["sessions"]]
    assert "good-session" in ids
    assert "bad-session-id" not in ids


def test_build_index_ignored_list_format(tmp_path):
    # Flat list format for ignored.json
    (tmp_path / "ignored.json").write_text(json.dumps(["bad-one"]))

    builder = IndexBuilder(tmp_path)
    good = _make_session("good-one")
    bad = _make_session("bad-one")
    builder.build_index([good, bad], {})

    data = json.loads((tmp_path / "index.json").read_text())
    ids = [e["id"] for e in data["sessions"]]
    assert "good-one" in ids
    assert "bad-one" not in ids


def test_build_index_invalid_ignored_json(tmp_path):
    # Corrupt ignored.json should not crash
    (tmp_path / "ignored.json").write_text("not valid json {{{")
    builder = IndexBuilder(tmp_path)
    session = _make_session()
    builder.build_index([session], {})  # should not raise

    data = json.loads((tmp_path / "index.json").read_text())
    assert len(data["sessions"]) == 1


def test_corrupt_ignored_json_warns_instead_of_silent_reset(tmp_path, caplog):
    """A corrupt ignore list must log a warning (#116).

    Silently returning an empty set made previously pruned sessions
    reappear in the next rebuild with zero diagnostic trail.
    """
    import logging

    (tmp_path / "ignored.json").write_text("not valid json {{{")
    builder = IndexBuilder(tmp_path)

    with caplog.at_level(logging.WARNING, logger="lore.exporters.index"):
        ignored = builder._load_ignored()

    assert ignored == set()
    assert any("ignored.json" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_unreadable_ignored_json_warns(tmp_path, caplog):
    """Unreadable (not just malformed) ignore lists take the same path."""
    import logging
    import os

    ignore_path = tmp_path / "ignored.json"
    ignore_path.write_text(json.dumps({"session_ids": ["x"]}))
    os.chmod(ignore_path, 0o000)
    builder = IndexBuilder(tmp_path)

    try:
        with caplog.at_level(logging.WARNING, logger="lore.exporters.index"):
            ignored = builder._load_ignored()
    finally:
        os.chmod(ignore_path, 0o644)

    assert ignored == set()
    assert any("ignored.json" in record.message for record in caplog.records)


def test_build_index_with_export_path(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session("exp-session")
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    export_p = export_dir / "exp-session.md"
    export_p.write_text("# Session")
    builder.build_index([session], {"exp-session": export_p})

    data = json.loads((tmp_path / "index.json").read_text())
    entry = data["sessions"][0]
    assert entry["export_path"] is not None
    assert "exp-session" in entry["export_path"]


# ---------------------------------------------------------------------------
# IndexBuilder._infer_title
# ---------------------------------------------------------------------------


def test_infer_title_uses_native_title(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(title="My Great Session Title Here")
    builder.build_index([session], {})

    data = json.loads((tmp_path / "index.json").read_text())
    assert "My Great Session Title Here" in data["sessions"][0]["title"]


def test_infer_title_falls_back_to_prompt(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(
        title=None, messages=[("user", "Implement a robust search engine with fuzzy matching")]
    )
    builder.build_index([session], {})

    data = json.loads((tmp_path / "index.json").read_text())
    title = data["sessions"][0]["title"]
    assert len(title) > 0


def test_infer_title_date_fallback_when_no_prompts(tmp_path):
    builder = IndexBuilder(tmp_path)
    # Only assistant messages - no usable first user prompt
    session = _make_session(title=None, messages=[("assistant", "How can I help you today?")])
    builder.build_index([session], {})

    data = json.loads((tmp_path / "index.json").read_text())
    title = data["sessions"][0]["title"]
    assert "2025" in title  # date-based fallback contains year


# ---------------------------------------------------------------------------
# IndexBuilder._is_low_quality_title
# ---------------------------------------------------------------------------


def test_is_low_quality_empty():
    builder = IndexBuilder(Path("/tmp"))
    assert builder._is_low_quality_title("") is True


def test_is_low_quality_short_text():
    builder = IndexBuilder(Path("/tmp"))
    assert builder._is_low_quality_title("hi") is True


def test_is_low_quality_caveat_prefix():
    builder = IndexBuilder(Path("/tmp"))
    assert (
        builder._is_low_quality_title("Caveat: the messages below were generated by the user")
        is True
    )


def test_is_low_quality_ok_title():
    builder = IndexBuilder(Path("/tmp"))
    assert builder._is_low_quality_title("Implement pytest coverage gate") is False


# ---------------------------------------------------------------------------
# IndexBuilder._compute_stats_from_entries
# ---------------------------------------------------------------------------


def test_compute_stats_from_entries_empty(tmp_path):
    builder = IndexBuilder(tmp_path)
    stats = builder._compute_stats_from_entries([])
    assert stats["total_sessions"] == 0
    assert stats["total_messages"] == 0


def test_compute_stats_from_entries_groups_by_tool(tmp_path):
    builder = IndexBuilder(tmp_path)
    entries = [
        {"tool": "warp", "project": "/p1", "messages": 3},
        {"tool": "warp", "project": "/p2", "messages": 5},
        {"tool": "claude-code", "project": None, "messages": 2},
    ]
    stats = builder._compute_stats_from_entries(entries)
    assert stats["by_tool"]["warp"] == 2
    assert stats["by_tool"]["claude-code"] == 1
    assert stats["total_messages"] == 10


# ---------------------------------------------------------------------------
# IndexBuilder._extract_keywords
# ---------------------------------------------------------------------------


def test_extract_keywords_limits_count(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(
        title="python flask database postgresql testing",
        messages=[("user", " ".join([f"keyword{i}" for i in range(200)]))],
    )
    keywords = builder._extract_keywords(session)
    assert len(keywords) <= 100


def test_extract_keywords_filters_stopwords(tmp_path):
    builder = IndexBuilder(tmp_path)
    session = _make_session(
        messages=[("user", "this that with have will from they been were said")]
    )
    keywords = builder._extract_keywords(session)
    stopwords = {"this", "that", "with", "have", "will", "from", "they", "been", "were", "said"}
    assert not any(kw in stopwords for kw in keywords)


# ---------------------------------------------------------------------------
# Multiple sessions / concurrent builds
# ---------------------------------------------------------------------------


def test_build_index_multiple_tools(tmp_path):
    builder = IndexBuilder(tmp_path)
    sessions = [
        _make_session(f"s{i}", tool, project_path=f"/proj/{i}")
        for i, tool in enumerate([Tool.CLAUDE_CODE, Tool.WARP, Tool.GEMINI_CLI, Tool.CODEX])
    ]
    builder.build_index(sessions, {})

    data = json.loads((tmp_path / "index.json").read_text())
    assert data["stats"]["total_sessions"] == 4
    assert len(data["stats"]["by_tool"]) == 4


def test_build_index_atomic_write_no_partial_file(tmp_path):
    """Verify the index.json doesn't have a partial write (temp file cleaned up)."""
    builder = IndexBuilder(tmp_path)
    session = _make_session()
    builder.build_index([session], {})

    # Temp files should be cleaned up
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0
