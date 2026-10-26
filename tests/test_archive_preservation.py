"""Index entries survive every filesystem state; only explicit removals drop them.

- Preservation never inspects the filesystem: a gone, unmounted or unreadable
  export or source keeps its index row (P1).
- Only ids in ``ignored.json`` (pruned sessions) or tombstones leave the index.
- A filtered export (``--tool``) keeps the rows of every other tool (P2).
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

import lore_cli
from lore.extractors.claude import ClaudeCodeExtractor
from lore.services.extraction import preserve_unobserved_index_entries
from lore.utils.home_discovery import _discover_cached
from tests.test_incremental_export import _records, _write


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
    return tmp_path, project


def _export(tmp_path: Path, *, tool: str | None = None) -> dict:
    out = tmp_path / "out"
    lore_cli.cmd_export(Namespace(output_dir=str(out), tool=tool, project=None, full=False))
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def _by_id(index: dict) -> dict:
    return {entry["id"]: entry for entry in index["sessions"]}


def _deny_under(monkeypatch, denied: Path) -> None:
    """Make os.stat raise PermissionError for any path under ``denied``."""
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path).startswith(str(denied)):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)


def test_gone_unreadable_or_unmounted_entries_are_kept(tmp_path, monkeypatch):
    """Permission-denied and unmounted paths never cost an index row."""
    locked = tmp_path / "locked"
    _deny_under(monkeypatch, locked)
    existing = {
        "sess-x": {
            "id": "sess-x",
            "tool": "claude-code",
            "export_path": str(locked / "out" / "sess-x.md"),
            "source_path": str(locked / "src" / "sess-x.jsonl"),
        },
        "sess-m": {
            "id": "sess-m",
            "tool": "claude-code",
            "export_path": str(tmp_path / "unplugged" / "out" / "sess-m.md"),
            "source_path": str(tmp_path / "unplugged" / "src" / "sess-m.jsonl"),
        },
    }

    kept = preserve_unobserved_index_entries(existing, set())

    assert sorted(entry["id"] for entry in kept) == ["sess-m", "sess-x"]


def test_both_gone_entry_is_kept_after_export(env, capsys):
    """Export and source gone and unseen -> still kept; no implicit garbage collection."""
    tmp_path, project = env
    first = _export(tmp_path)
    Path(_by_id(first)["sess-b"]["export_path"]).unlink()
    (project / "sess-b.jsonl").unlink()

    second = _export(tmp_path)

    assert sorted(_by_id(second)) == ["sess-a", "sess-b", "sess-c"]
    assert "Dropped" not in capsys.readouterr().err


def test_missing_export_with_existing_source_is_kept(env, capsys):
    """Only a gone export is not enough; the source still exists, so keep."""
    tmp_path, _project = env
    first = _export(tmp_path)
    Path(_by_id(first)["sess-b"]["export_path"]).unlink()

    second = _export(tmp_path)

    assert "sess-b" in _by_id(second)
    assert "Dropped" not in capsys.readouterr().err


def test_filtered_export_keeps_other_tools_entries(env):
    """--tool claude-code must not rewrite the index without other tools' rows."""
    tmp_path, _project = env
    first = _export(tmp_path)

    out = tmp_path / "out"
    other_export = out / "other" / "codex-1.md"
    other_source = tmp_path / "codex-1.jsonl"
    other_export.parent.mkdir(parents=True, exist_ok=True)
    other_export.write_text("# codex", encoding="utf-8")
    other_source.write_text("{}", encoding="utf-8")
    index_file = out / "index.json"
    payload = json.loads(index_file.read_text(encoding="utf-8"))
    foreign = dict(_by_id(first)["sess-a"])
    foreign.update(
        id="codex-1",
        tool="codex",
        export_path=str(other_export),
        source_path=str(other_source),
    )
    payload["sessions"].append(foreign)
    index_file.write_text(json.dumps(payload), encoding="utf-8")

    filtered = _export(tmp_path, tool="claude-code")

    assert "codex-1" in _by_id(filtered)
    assert _by_id(filtered)["codex-1"]["export_path"] == str(other_export)
    assert sorted(_by_id(filtered)) == sorted([*_by_id(first), "codex-1"])


def test_filtered_export_keeps_gone_entries_of_filtered_tool(env, capsys):
    """A --tool run does not garbage-collect the filtered tool's gone entries either."""
    tmp_path, project = env
    first = _export(tmp_path)
    Path(_by_id(first)["sess-c"]["export_path"]).unlink()
    (project / "sess-c.jsonl").unlink()

    filtered = _export(tmp_path, tool="claude-code")

    assert "sess-c" in _by_id(filtered)
    assert "Dropped" not in capsys.readouterr().err


def test_ignored_and_tombstoned_ids_are_removed_by_preserve(tmp_path):
    """Only ignored ids (pruned) and tombstones leave; everything else stays."""
    existing = {
        "pruned-1": {"id": "pruned-1", "tool": "codex", "export_path": str(tmp_path / "a.md")},
        "pruned-2": {"id": "pruned-2", "tool": "claude-code"},
        "tombstone": {"id": "tombstone", "tool": "claude-code"},
        "observed": {"id": "observed", "tool": "claude-code"},
        "offline": {"id": "offline", "tool": "codex", "source_path": str(tmp_path / "gone")},
    }

    kept = preserve_unobserved_index_entries(
        existing,
        {"observed"},
        ignored_ids={"pruned-1", "pruned-2"},
        deleted_ids={"tombstone"},
    )

    assert [entry["id"] for entry in kept] == ["offline"]
