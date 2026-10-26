"""Index entries survive filesystem trouble; only confirmed-gone rows are pruned.

- An unreadable or unmounted archive must never remove index rows (P1).
- Only an entry whose export and source are both positively absent, and that
  was not seen this run, is dropped, and the drop is reported on stderr.
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


def test_permission_denied_parent_keeps_entry(tmp_path, monkeypatch):
    """A missing-looking file under an unreadable dir is unknown, so it is kept."""
    locked = tmp_path / "locked"
    export = locked / "out" / "sess-x.md"
    source = locked / "src" / "sess-x.jsonl"
    existing = {
        "sess-x": {
            "id": "sess-x",
            "tool": "claude-code",
            "export_path": str(export),
            "source_path": str(source),
        }
    }
    _deny_under(monkeypatch, locked)
    pruned: list[str] = []

    kept = preserve_unobserved_index_entries(existing, set(), pruned=pruned)

    assert [entry["id"] for entry in kept] == ["sess-x"]
    assert pruned == []


def test_permission_error_on_existing_file_keeps_entry(tmp_path, monkeypatch):
    """A file that exists but cannot be stat'ed is unknown, never absent."""
    locked = tmp_path / "locked"
    export = locked / "sess-y.md"
    source = locked / "sess-y.jsonl"
    locked.mkdir()
    export.write_text("x", encoding="utf-8")
    existing = {
        "sess-y": {
            "id": "sess-y",
            "tool": "claude-code",
            "export_path": str(export),
            "source_path": str(source),
        }
    }
    _deny_under(monkeypatch, locked)
    pruned: list[str] = []

    kept = preserve_unobserved_index_entries(existing, set(), pruned=pruned)

    assert [entry["id"] for entry in kept] == ["sess-y"]
    assert pruned == []


def test_unmounted_archive_keeps_entry(tmp_path):
    """Missing mount point (parent absent too) is not positive absence."""
    existing = {
        "sess-m": {
            "id": "sess-m",
            "tool": "claude-code",
            "export_path": str(tmp_path / "unplugged" / "out" / "sess-m.md"),
            "source_path": str(tmp_path / "unplugged" / "src" / "sess-m.jsonl"),
        }
    }
    pruned: list[str] = []

    kept = preserve_unobserved_index_entries(existing, set(), pruned=pruned)

    assert [entry["id"] for entry in kept] == ["sess-m"]
    assert pruned == []


def test_both_confirmed_missing_is_dropped_and_reported(env, capsys):
    """Export and source confirmed gone and unseen -> dropped with a stderr count."""
    tmp_path, project = env
    first = _export(tmp_path)
    entry = _by_id(first)["sess-b"]
    Path(entry["export_path"]).unlink()
    (project / "sess-b.jsonl").unlink()

    second = _export(tmp_path)

    assert "sess-b" not in _by_id(second)
    assert sorted(_by_id(second)) == ["sess-a", "sess-c"]
    err = capsys.readouterr().err
    assert "Dropped 1 index entries whose export and source are both gone." in err


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


def test_filtered_export_still_prunes_gone_entries_of_filtered_tool(env, capsys):
    """Entries of the filtered tool follow the same confirmed-gone rule."""
    tmp_path, project = env
    first = _export(tmp_path)
    Path(_by_id(first)["sess-c"]["export_path"]).unlink()
    (project / "sess-c.jsonl").unlink()

    filtered = _export(tmp_path, tool="claude-code")

    assert "sess-c" not in _by_id(filtered)
    assert "Dropped 1 index entries whose export and source are both gone." in (
        capsys.readouterr().err
    )


def test_ignored_id_is_not_resurrected_by_preserve(tmp_path):
    """ignored_ids wins for any tool, including the filter's other tools."""
    export = tmp_path / "gone.md"
    source = tmp_path / "gone.jsonl"
    existing = {
        "pruned-1": {"id": "pruned-1", "tool": "codex", "export_path": str(export)},
        "pruned-2": {
            "id": "pruned-2",
            "tool": "claude-code",
            "export_path": str(export),
            "source_path": str(source),
        },
    }

    kept = preserve_unobserved_index_entries(
        existing,
        set(),
        ignored_ids={"pruned-1", "pruned-2"},
        tool_filter="claude-code",
    )

    assert kept == []
