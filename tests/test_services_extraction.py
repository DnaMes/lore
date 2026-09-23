"""Tests for services.extraction — the Sync engine (#28 QA-gap).

build_search_index drives the Sync button: extractor iteration, the
incremental mtime-reuse decision, cancellation, and per-extractor error
collection. The web reload tests stub this out, so it is exercised
directly here with stub extractors.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services import extraction


def _session(sid, source_path=None, n=2):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid} realistic title",
        source_path=source_path,
        messages=[
            UnifiedMessage(role=Role.USER, content=f"msg {i}", timestamp=datetime(2026, 1, 1))
            for i in range(n)
        ],
    )


class _StubExtractor:
    """A BaseExtractor-shaped stub for the build pipeline."""

    def __init__(self, tool, sessions, *, available=True, raises=None):
        self._tool = tool
        self._sessions = sessions
        self._available = available
        self._raises = raises

    @property
    def tool(self):
        return self._tool

    def is_available(self):
        return self._available

    def extract_sessions(self):
        if self._raises is not None:
            raise self._raises
        yield from self._sessions


@pytest.fixture()
def patched_extractors(monkeypatch):
    """Return a setter that controls what get_all_extractors() yields."""

    def _set(extractors):
        monkeypatch.setattr(extraction, "get_all_extractors", lambda: extractors)

    return _set


# ---------------------------------------------------------------------------
# Basic build
# ---------------------------------------------------------------------------


def test_build_indexes_extracted_sessions(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("a"), _session("b")])])
    errors = extraction.build_search_index(tmp_path, tmp_path / "index.json")
    assert errors == []
    assert (tmp_path / "index.json").exists()
    payload = json.loads((tmp_path / "index.json").read_text())
    assert sorted(s["id"] for s in payload["sessions"]) == ["a", "b"]


def test_build_skips_unavailable_extractor(tmp_path, patched_extractors):
    patched_extractors(
        [
            _StubExtractor(Tool.CLAUDE_CODE, [_session("a")]),
            _StubExtractor(Tool.CODEX, [_session("z")], available=False),
        ]
    )
    extraction.build_search_index(tmp_path, tmp_path / "index.json")
    payload = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in payload["sessions"]] == ["a"]


# ---------------------------------------------------------------------------
# Error collection — a throwing extractor must not abort the build
# ---------------------------------------------------------------------------


def test_throwing_extractor_is_collected_not_fatal(tmp_path, patched_extractors):
    patched_extractors(
        [
            _StubExtractor(Tool.CODEX, [], raises=RuntimeError("extractor exploded")),
            _StubExtractor(Tool.CLAUDE_CODE, [_session("survivor")]),
        ]
    )
    errors = extraction.build_search_index(tmp_path, tmp_path / "index.json")
    # The good extractor's session still made it in.
    payload = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in payload["sessions"]] == ["survivor"]
    # The failure was recorded, not raised.
    assert len(errors) == 1
    assert errors[0]["extractor"] == "codex"
    assert "exploded" in errors[0]["error"]


def test_failed_extractor_preserves_prior_entries(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [], raises=RuntimeError("gone"))])

    errors = extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)

    payload = json.loads((tmp_path / "index.json").read_text())
    assert [row["id"] for row in payload["sessions"]] == ["old"]
    assert errors == [{"extractor": "claude-code", "error": "gone"}]
    conn = sqlite3.connect(tmp_path / "index_v2.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='old'").fetchone() == (2,)


def test_empty_extractor_preserves_prior_entries_and_adds_healthy_rows(
    tmp_path, patched_extractors
):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors(
        [_StubExtractor(Tool.CLAUDE_CODE, []), _StubExtractor(Tool.CODEX, [_session("new")])]
    )

    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)

    ids = [row["id"] for row in json.loads((tmp_path / "index.json").read_text())["sessions"]]
    assert sorted(ids) == ["new", "old"]
    conn = sqlite3.connect(tmp_path / "index_v2.sqlite")
    assert {row[0] for row in conn.execute("SELECT id FROM sessions")} == {"new", "old"}


def test_partial_extractor_preserves_unseen_prior_entries(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old-a"), _session("old-b")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old-a")])])

    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)

    ids = [row["id"] for row in json.loads((tmp_path / "index.json").read_text())["sessions"]]
    assert sorted(ids) == ["old-a", "old-b"]


def test_explicit_tombstone_is_not_reintroduced_while_other_entries_survive(
    tmp_path, patched_extractors
):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("keep"), _session("drop")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [])])

    extraction.build_search_index(
        tmp_path, tmp_path / "index.json", deleted_ids={"drop"}, incremental=False
    )

    ids = [row["id"] for row in json.loads((tmp_path / "index.json").read_text())["sessions"]]
    assert ids == ["keep"]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_should_stop_raises_cancelled(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("a")])])
    with pytest.raises(extraction.ActionJobCancelledError):
        extraction.build_search_index(tmp_path, tmp_path / "index.json", should_stop=lambda: True)


def test_cancelled_build_leaves_previous_indexes_untouched(tmp_path, patched_extractors):
    """Cancellation rolls back the in-flight v2 transaction and JSON write."""
    from lore.storage import v2_db_path

    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    before = (tmp_path / "index.json").read_bytes()

    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("new")])])
    with pytest.raises(extraction.ActionJobCancelledError):
        extraction.build_search_index(
            tmp_path, tmp_path / "index.json", incremental=False, should_stop=lambda: True
        )

    assert (tmp_path / "index.json").read_bytes() == before
    conn = sqlite3.connect(v2_db_path(tmp_path))
    assert [row[0] for row in conn.execute("SELECT id FROM sessions")] == ["old"]


# ---------------------------------------------------------------------------
# Incremental mtime reuse
# ---------------------------------------------------------------------------


def test_incremental_reuses_unchanged_session(tmp_path, patched_extractors):
    """A session whose source-file mtime is unchanged is reused, not
    re-extracted."""
    # A real source file so _stat_mtime_ns has something to read.
    src = tmp_path / "session-source.jsonl"
    src.write_text("{}", encoding="utf-8")

    sess = _session("reused-1", source_path=str(src))
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [sess])])

    # First build — establishes the index with the session's source_mtime.
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=True)
    payload1 = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in payload1["sessions"]] == ["reused-1"]

    # Second build, source file untouched -> the session is reused verbatim.
    progress = []
    extraction.build_search_index(
        tmp_path,
        tmp_path / "index.json",
        incremental=True,
        progress_callback=lambda pct, msg: progress.append(msg),
    )
    payload2 = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in payload2["sessions"]] == ["reused-1"]
    # The final progress message reports it as reused, not refreshed.
    assert any("1 reused" in m for m in progress)


def test_incremental_reextracts_changed_session(tmp_path, patched_extractors):
    """A changed source-file mtime forces re-extraction (not reuse)."""
    import os
    import time

    src = tmp_path / "session-source.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = _session("changing-1", source_path=str(src))
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [sess])])

    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=True)

    # Bump the source mtime so it no longer matches the indexed value.
    time.sleep(0.01)
    new_time = time.time() + 100
    os.utime(src, (new_time, new_time))

    progress = []
    extraction.build_search_index(
        tmp_path,
        tmp_path / "index.json",
        incremental=True,
        progress_callback=lambda pct, msg: progress.append(msg),
    )
    # Changed -> refreshed, not reused.
    assert any("1 refreshed" in m for m in progress)


def test_deleted_ids_are_filtered_out(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("keep"), _session("drop")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", deleted_ids={"drop"})
    payload = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in payload["sessions"]] == ["keep"]
