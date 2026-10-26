"""Prune must only unlink export_path files inside the archive root."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path

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
