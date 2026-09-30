"""``lore export`` must not re-parse Claude transcripts that did not change.

Every lore-sync run used to parse every JSONL file again (1300 sessions,
1.3 M JSON lines, ~20 CPU minutes). The existing index already records each
session's ``source_path`` and ``source_mtime``; the export now consults it
*before* parsing, reuses the prior index entry for unchanged files and parses
only what changed. ``--full`` keeps the old behaviour as a safety valve.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

import lore_cli
from lore.extractors.claude import ClaudeCodeExtractor
from lore.utils.home_discovery import _discover_cached, discover_home_marker_paths


def _records(session_id: str, extra: int = 0) -> list[dict]:
    rows: list[dict] = []
    for i in range(3 + extra):
        ts = f"2025-06-15T10:{i:02d}:00"
        rows.append(
            {
                "type": "user",
                "timestamp": ts,
                "sessionId": session_id,
                "message": {"role": "user", "content": f"{session_id} question {i}"},
                "uuid": f"{session_id}-u{i}",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "timestamp": ts,
                "sessionId": session_id,
                "message": {"role": "assistant", "content": f"{session_id} answer {i}"},
                "uuid": f"{session_id}-a{i}",
            }
        )
    return rows


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _discover_cached.cache_clear()
    project = tmp_path / ".claude" / "projects" / "-home-user-app"
    project.mkdir(parents=True)
    for name in ("sess-a", "sess-b", "sess-c"):
        _write(project / f"{name}.jsonl", _records(name))

    extractor = ClaudeCodeExtractor()
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [extractor])

    parsed: list[str] = []
    original = ClaudeCodeExtractor._parse_session

    def counting(self, path, *args, **kwargs):
        parsed.append(Path(path).stem)
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(ClaudeCodeExtractor, "_parse_session", counting)
    return tmp_path, project, parsed


def _export(tmp_path: Path, *, full: bool = False) -> dict:
    out = tmp_path / "out"
    lore_cli.cmd_export(Namespace(output_dir=str(out), tool=None, project=None, full=full))
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def _ids(index: dict) -> list[str]:
    return sorted(entry["id"] for entry in index["sessions"])


def test_unchanged_sources_are_not_parsed_again(env):
    tmp_path, _, parsed = env
    first = _export(tmp_path)
    assert sorted(parsed) == ["sess-a", "sess-b", "sess-c"]

    parsed.clear()
    second = _export(tmp_path)

    assert parsed == [], f"unchanged files were parsed again: {parsed}"
    assert _ids(second) == _ids(first) == ["sess-a", "sess-b", "sess-c"]


def test_only_the_changed_source_is_parsed_and_refreshed(env):
    tmp_path, project, parsed = env
    _export(tmp_path)

    target = project / "sess-b.jsonl"
    _write(target, _records("sess-b", extra=2))
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    parsed.clear()
    index = _export(tmp_path)

    assert parsed == ["sess-b"]
    assert _ids(index) == ["sess-a", "sess-b", "sess-c"]
    messages = {entry["id"]: entry["messages"] for entry in index["sessions"]}
    assert messages["sess-b"] == 10
    assert messages["sess-a"] == 6


def test_source_touched_during_the_previous_scan_is_reparsed(env):
    """A file that grew while the last scan ran may have been parsed partially.

    The index stamps ``source_mtime`` when it writes the row, which can be after
    the file was parsed. Such a row must not count as up to date, or the tail of
    a live session would be lost until its next write.
    """
    tmp_path, project, parsed = env
    _export(tmp_path)

    index_path = tmp_path / "out" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    target = project / "sess-c.jsonl"
    _write(target, _records("sess-c", extra=1))  # mtime is now after generated_at
    for entry in index["sessions"]:
        if entry["id"] == "sess-c":
            entry["source_mtime"] = target.stat().st_mtime_ns  # stamped "up to date"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    parsed.clear()
    refreshed = _export(tmp_path)

    assert parsed == ["sess-c"]
    assert {e["id"]: e["messages"] for e in refreshed["sessions"]}["sess-c"] == 8


def test_full_flag_reparses_everything(env):
    tmp_path, _, parsed = env
    _export(tmp_path)

    parsed.clear()
    index = _export(tmp_path, full=True)

    assert sorted(parsed) == ["sess-a", "sess-b", "sess-c"]
    assert _ids(index) == ["sess-a", "sess-b", "sess-c"]


def test_duplicate_session_id_in_second_project_does_not_duplicate_index_row(env):
    tmp_path, project, parsed = env
    _export(tmp_path)

    # Same sessionId resumed from another cwd: a second, newer copy appears.
    other = project.parent / "-home-user-other"
    other.mkdir()
    copy = other / "sess-a.jsonl"
    _write(copy, _records("sess-a", extra=1))
    stat = copy.stat()
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    parsed.clear()
    index = _export(tmp_path)

    assert _ids(index) == ["sess-a", "sess-b", "sess-c"], "duplicate id in index"
    assert {e["id"]: e["messages"] for e in index["sessions"]}["sess-a"] == 8


def test_home_scan_survives_unreadable_entries(tmp_path, monkeypatch):
    """A dir we can list but not stat into used to crash the whole export."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _discover_cached.cache_clear()
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o600)  # r-- but no x: iterdir works, stat of children fails
    try:
        found = discover_home_marker_paths(".claude/projects")
    finally:
        locked.chmod(0o700)
    assert tmp_path / ".claude" / "projects" in found
