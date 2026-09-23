"""Streaming contract for ``lore export --all`` (lore-sync memory peak).

``cmd_export`` used to append every ``UnifiedSession`` (with all message
bodies) to a list before building the index, so a full sync held the whole
archive in RAM (~3 GB for ~1000 sessions). ``IndexBuilder.build_index`` already
streams; these tests lock in that ``cmd_export`` feeds it a stream too, and that
the exported files and index stay identical.
"""

from __future__ import annotations

import gc
import json
import weakref
from argparse import Namespace
from datetime import datetime

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession

TOTAL = 40


def _session(session_id: str, project: str) -> UnifiedSession:
    now = datetime(2026, 3, 4, 12, 0, 0)
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=[
            UnifiedMessage(role=Role.USER, content=f"hello from {session_id}", timestamp=now),
            UnifiedMessage(role=Role.ASSISTANT, content="hi there", timestamp=now),
        ],
        project_path=project,
        title=f"Session {session_id}",
    )


class _FakeExtractor:
    """Yields TOTAL sessions and records how many are still alive per step."""

    tool = Tool.CLAUDE_CODE

    def __init__(self, project: str) -> None:
        self._project = project
        self.refs: list[weakref.ref] = []
        self.peak_alive = 0

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        for i in range(TOTAL):
            gc.collect()
            alive = sum(1 for ref in self.refs if ref() is not None)
            self.peak_alive = max(self.peak_alive, alive)
            session = _session(f"sess-{i:03d}", self._project)
            self.refs.append(weakref.ref(session))
            yield session
            del session


def _run_export(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    out = tmp_path / "out"
    extractor = _FakeExtractor(str(project))
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])
    lore_cli.cmd_export(Namespace(output_dir=str(out), tool=None, project=None))
    return out, extractor


def test_export_all_does_not_retain_every_session(tmp_path, monkeypatch):
    """A full export must keep only a small constant number of sessions alive."""
    _, extractor = _run_export(tmp_path, monkeypatch)

    assert extractor.peak_alive < TOTAL // 2, (
        f"peak alive sessions {extractor.peak_alive}/{TOTAL} — cmd_export "
        "is materialising the full session list"
    )


def test_export_all_still_writes_markdown_and_index(tmp_path, monkeypatch):
    """Streaming must not change what lands on disk or in the index."""
    out, _ = _run_export(tmp_path, monkeypatch)

    entries = json.loads((out / "index.json").read_text())["sessions"]
    assert {e["id"] for e in entries} == {f"sess-{i:03d}" for i in range(TOTAL)}
    assert all(e.get("export_path") for e in entries), "every entry needs its export_path"
    assert len(list(out.rglob("*.md"))) == TOTAL


def test_export_all_reports_session_count(tmp_path, monkeypatch, capsys):
    """The summary line still reports how many sessions were exported."""
    _run_export(tmp_path, monkeypatch)

    assert f"Exported {TOTAL} sessions" in capsys.readouterr().out
