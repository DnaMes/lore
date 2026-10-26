"""Prune must only unlink export_path files inside the archive root."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.extractors.base import BaseExtractor
from lore.storage import load_index_v2, search_sessions


def _session(sid: str, prompts: int = 1) -> UnifiedSession:
    messages = []
    for i in range(prompts):
        messages.append(
            UnifiedMessage(role=Role.USER, content=f"ask {i}", timestamp=datetime(2026, 1, 1))
        )
        messages.append(
            UnifiedMessage(role=Role.ASSISTANT, content=f"ans {i}", timestamp=datetime(2026, 1, 1))
        )
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/proj",
        messages=messages,
    )


class _EmptyExtractor(BaseExtractor):
    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        return iter(())


def _prune_args(output_dir: Path, **extra) -> Namespace:
    base = {
        "output_dir": str(output_dir),
        "max_prompts": 3,
        "dry_run": False,
        "include_automated": False,
    }
    base.update(extra)
    return Namespace(**base)


def test_prune_refuses_export_path_outside_archive_root(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "lore"
    archive.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("do not delete", encoding="utf-8")

    IndexBuilder(archive).build_index([_session("out-sess")], {})
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "out-sess":
            entry["export_path"] = str(outside)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "do not delete"
    err = capsys.readouterr().err
    assert "refusing to delete" in err
    assert "outside" in err


def test_prune_refuses_symlink_escape(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "lore"
    projects = archive / "projects" / "p"
    projects.mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("keep me", encoding="utf-8")
    link = projects / "escape.md"
    link.symlink_to(outside)

    IndexBuilder(archive).build_index([_session("link-sess")], {})
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "link-sess":
            entry["export_path"] = str(link)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert "refusing to delete" in capsys.readouterr().err


def test_pruned_session_absent_from_v2_list_and_search_after_rebuild(tmp_path, monkeypatch):
    keep = _session("keep-me", prompts=5)
    drop = _session("drop-me", prompts=1)
    for msg in drop.messages:
        msg.content = "unique-prune-token " + msg.content
    IndexBuilder(tmp_path).build_index([keep, drop], {})
    assert {s["id"] for s in load_index_v2(tmp_path)["sessions"]} == {"keep-me", "drop-me"}

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "drop-me":
            entry["prompts"] = 1
        if entry["id"] == "keep-me":
            entry["prompts"] = 5
    (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")

    class _KeepExtractor(BaseExtractor):
        @property
        def tool(self) -> Tool:
            return Tool.CLAUDE_CODE

        def is_available(self) -> bool:
            return True

        def extract_sessions(self):
            yield keep

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_KeepExtractor()])
    lore_cli.cmd_prune(_prune_args(tmp_path))

    ids = {s["id"] for s in load_index_v2(tmp_path)["sessions"]}
    assert "drop-me" not in ids
    assert "keep-me" in ids
    hits = search_sessions(tmp_path, "unique-prune-token")
    assert all(h["session"]["id"] != "drop-me" for h in hits)


def _archive_with_candidate(tmp_path: Path, target: Path) -> Path:
    """Index one low-prompt session whose export_path points at ``target``."""
    archive = tmp_path / "lore"
    archive.mkdir(exist_ok=True)
    IndexBuilder(archive).build_index([_session("out-sess")], {})
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "out-sess":
            entry["export_path"] = str(target)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return archive


@pytest.mark.parametrize(
    "relative",
    [
        "index.json",
        "index_v2.sqlite",
        "ignored.json",
        "deleted_sessions.json",
        "rules.md",
        "projects/p/notes.txt",
        "projects/p/data.json",
        "projects/p/store.db",
        "projects/p/cache.sqlite-wal",
    ],
)
def test_prune_refuses_store_control_and_non_md_targets(tmp_path, monkeypatch, capsys, relative):
    target = tmp_path / "lore" / relative
    archive = _archive_with_candidate(tmp_path, target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("keep me", encoding="utf-8")
    payload = target.read_bytes() if target.exists() else b""

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert target.exists()
    if target.name not in {"index.json", "ignored.json", "index_v2.sqlite"}:
        assert target.read_bytes() == payload  # prune rewrites the three above
    assert "refusing to delete" in capsys.readouterr().err


def test_prune_refuses_real_index_db_even_when_export_points_at_it(tmp_path, monkeypatch, capsys):
    """The primary store itself must survive a manipulated export_path."""
    archive = tmp_path / "lore"
    archive.mkdir()
    IndexBuilder(archive).build_index([_session("out-sess")], {})
    store = archive / "index_v2.sqlite"
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "out-sess":
            entry["export_path"] = str(store)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert store.read_bytes()[:16] == b"SQLite format 3\x00"
    assert "refusing to delete" in capsys.readouterr().err


def test_prune_refuses_symlink_to_real_file_even_inside_root(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "lore"
    projects = archive / "projects" / "p"
    projects.mkdir(parents=True)
    real = projects / "real.md"
    real.write_text("real export", encoding="utf-8")
    link = projects / "alias.md"
    link.symlink_to(real)
    archive = _archive_with_candidate(tmp_path, link)

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert real.exists()
    assert link.is_symlink()
    assert "refusing to delete" in capsys.readouterr().err


def test_prune_deletes_regular_md_export_under_projects(tmp_path, monkeypatch):
    archive = tmp_path / "lore"
    project = archive / "projects" / "p"
    project.mkdir(parents=True)
    export = project / "2026-01-01_title_out-sess.md"
    export.write_text("# export", encoding="utf-8")
    _archive_with_candidate(tmp_path, export)

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    assert not export.exists()


def test_prune_keeps_offline_entry_and_removes_only_pruned_id(tmp_path, monkeypatch):
    """Sessions whose source is not extracted now survive; the pruned id does not."""
    archive = tmp_path / "lore"
    archive.mkdir()
    IndexBuilder(archive).build_index(
        [_session("keep-me", prompts=5), _session("offline-keep", prompts=5), _session("drop-me")],
        {},
    )
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        entry["prompts"] = 1 if entry["id"] == "drop-me" else 5
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")

    class _KeepExtractor(_EmptyExtractor):
        def extract_sessions(self):
            yield _session("keep-me", prompts=5)

    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_KeepExtractor()])
    lore_cli.cmd_prune(_prune_args(archive))

    ids = {
        s["id"]
        for s in json.loads((archive / "index.json").read_text(encoding="utf-8"))["sessions"]
    }
    assert ids == {"keep-me", "offline-keep"}
    assert "drop-me" in json.loads((archive / "ignored.json").read_text(encoding="utf-8"))
