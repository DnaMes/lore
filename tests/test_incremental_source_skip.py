"""Pre-parse source skipping in ``build_search_index`` (#112).

An unchanged source must be skipped *before* it is opened — but only when the
index build hands the extractor that knowledge, and never at the cost of data:
the prior JSON entry, the v2 message rows and the FTS row of a skipped session
survive verbatim, a missing source keeps its entry without a tombstone, and a
forced refresh (`incremental=False`) always re-parses.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import REEXTRACT_BUDGET_ENV, BaseExtractor
from lore.services import extraction
from lore.storage.writer import v2_db_path


def _session(sid: str, *, body: str, source_path: str | None = None) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid} realistic title",
        source_path=source_path,
        messages=[
            UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1)),
            UnifiedMessage(
                role=Role.ASSISTANT, content=f"reply {sid}", timestamp=datetime(2026, 1, 1)
            ),
        ],
    )


class _FileExtractor(BaseExtractor):
    """One session per JSON source file, honouring the pre-parse skip (#112)."""

    PARSER_VERSION = 1

    def __init__(self, root: Path) -> None:
        self._root = root
        self.parses = 0

    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        for path in sorted(self._root.glob("*.json")):
            if self._source_unchanged(path):
                continue
            self.parses += 1
            data = json.loads(path.read_text(encoding="utf-8"))
            yield _session(data["id"], body=data["body"], source_path=str(path))


def _write_source(root: Path, name: str, *, sid: str, body: str, mtime_ns: int) -> Path:
    path = root / name
    path.write_text(json.dumps({"id": sid, "body": body}), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _build(tmp_path: Path, monkeypatch, *, extractor: _FileExtractor, incremental: bool = True):
    monkeypatch.setattr(extraction, "get_all_extractors", lambda: [extractor])
    return extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=incremental)


def _index_ids(tmp_path: Path) -> list[str]:
    payload = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    return sorted(str(entry["id"]) for entry in payload["sessions"])


def _index_body(tmp_path: Path, sid: str) -> str:
    payload = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    entry = next(e for e in payload["sessions"] if e["id"] == sid)
    return json.dumps(entry)


def _message_count(tmp_path: Path, sid: str) -> int:
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
        )
    finally:
        conn.close()


def _fts_rows(tmp_path: Path, sid: str) -> int:
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM search_index WHERE entity_type = 'session' AND entity_id = ?",
                (sid,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _tombstones(tmp_path: Path) -> list[str]:
    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        return [r[0] for r in conn.execute("SELECT session_id FROM deleted_sessions")]
    finally:
        conn.close()


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    root = tmp_path / "sources"
    root.mkdir()
    _write_source(
        root, "s1.json", sid="s1", body="original body", mtime_ns=1_700_000_000_000_000_000
    )
    return root


def test_unchanged_source_is_skipped_before_open_and_state_survives(tmp_path, monkeypatch, source):
    out = tmp_path / "out"
    out.mkdir()
    cold = _FileExtractor(source)
    _build(out, monkeypatch, extractor=cold)
    assert cold.parses == 1

    warm = _FileExtractor(source)
    _build(out, monkeypatch, extractor=warm)

    assert warm.parses == 0  # never opened: the whole point of #112
    assert _index_ids(out) == ["s1"]
    assert "original body" in _index_body(out, "s1")
    assert _message_count(out, "s1") == 2  # v2 rows untouched, not emptied
    assert _fts_rows(out, "s1") == 1  # still searchable
    assert _tombstones(out) == []


def test_touched_source_is_reparsed_with_its_new_content(tmp_path, monkeypatch, source):
    out = tmp_path / "out"
    out.mkdir()
    _build(out, monkeypatch, extractor=_FileExtractor(source))

    _write_source(
        source, "s1.json", sid="s1", body="changed body", mtime_ns=1_700_000_100_000_000_000
    )
    warm = _FileExtractor(source)
    _build(out, monkeypatch, extractor=warm)

    assert warm.parses == 1
    assert "changed body" in _index_body(out, "s1")


def test_same_mtime_change_keeps_prior_content_and_loses_no_messages(tmp_path, monkeypatch, source):
    """mtime is the cache key: same stamp → skipped, and skipping must not cost rows (#112)."""
    out = tmp_path / "out"
    out.mkdir()
    _build(out, monkeypatch, extractor=_FileExtractor(source))

    _write_source(
        source, "s1.json", sid="s1", body="invisible rewrite", mtime_ns=1_700_000_000_000_000_000
    )
    warm = _FileExtractor(source)
    _build(out, monkeypatch, extractor=warm)

    assert warm.parses == 0
    assert "original body" in _index_body(out, "s1")  # stale by design: mtime unchanged
    assert _message_count(out, "s1") == 2  # the rows a wrong skip used to empty (#35)


def test_parser_version_bump_forces_a_repars(tmp_path, monkeypatch, source):
    monkeypatch.setenv(REEXTRACT_BUDGET_ENV, "10")
    out = tmp_path / "out"
    out.mkdir()
    _build(out, monkeypatch, extractor=_FileExtractor(source))

    class _V2Extractor(_FileExtractor):
        PARSER_VERSION = 2

    warm = _V2Extractor(source)
    _build(out, monkeypatch, extractor=warm)

    assert warm.parses == 1  # stored version 1 < 2: re-parse, fresh wins
    assert _index_ids(out) == ["s1"]


def test_deleted_source_keeps_its_entry_without_a_tombstone(tmp_path, monkeypatch, source):
    out = tmp_path / "out"
    out.mkdir()
    _build(out, monkeypatch, extractor=_FileExtractor(source))

    next(source.glob("*.json")).unlink()
    warm = _FileExtractor(source)
    _build(out, monkeypatch, extractor=warm)

    assert warm.parses == 0
    assert _index_ids(out) == ["s1"]  # preserved, not dropped
    assert _message_count(out, "s1") == 2
    assert _tombstones(out) == []  # only an explicit delete may tombstone


def test_forced_refresh_never_takes_the_skip(tmp_path, monkeypatch, source):
    out = tmp_path / "out"
    out.mkdir()
    _build(out, monkeypatch, extractor=_FileExtractor(source))

    forced = _FileExtractor(source)
    _build(out, monkeypatch, extractor=forced, incremental=False)

    assert forced.parses == 1
    assert _index_ids(out) == ["s1"]
