"""``lore memory import-claude``: dry run, idempotent apply, supersede, redaction (#44)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Optional

import pytest

import lore.storage.embeddings as emb
import lore_cli
from lore.services.claude_memory_import import import_claude_memory
from lore.storage import get_memory, list_memory
from lore.storage.writer import v2_db_path

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.fixture(autouse=True)
def no_backend(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed_text", lambda _t: None)
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)


def _memory_file(
    root: Path,
    slug: str,
    name: str,
    *,
    mtype: Optional[str] = "feedback",
    description: str = "Short description",
    body: str = "The rule body.",
) -> Path:
    folder = root / slug / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    type_line = f"  type: {mtype}\n" if mtype else ""
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata: \n"
        "  node_type: memory\n"
        f"{type_line}"
        "---\n\n"
        f"{body}\n"
    )
    path = folder / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _make_tree(tmp_path: Path) -> Path:
    """A Claude projects root with one feedback, one project, one reference and an index."""
    root = tmp_path / "claude-projects"
    _memory_file(root, "-some-project", "rule-one", mtype="feedback")
    _memory_file(root, "-some-project", "state-two", mtype="project", body="Project state.")
    _memory_file(root, "-other", "pointer", mtype="reference", body="See the tracker.")
    (root / "-some-project" / "memory" / "MEMORY.md").write_text(
        "# Memory Index\n\n- [rule-one](rule-one.md) — index line\n", encoding="utf-8"
    )
    return root


def _args(tmp_path: Path, root: Path, apply: bool) -> Namespace:
    return Namespace(
        output_dir=str(tmp_path / "store"),
        memory_action="import-claude",
        root=str(root),
        apply=apply,
    )


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def test_dry_run_writes_nothing(tmp_path, capsys):
    root = _make_tree(tmp_path)
    lore_cli.cmd_memory(_args(tmp_path, root, apply=False))

    assert not v2_db_path(_store(tmp_path)).exists()
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "Would write: 3 new" in out
    assert "MEMORY.md index 1" in out
    assert "Short description" not in out and "rule body" not in out


def test_apply_writes_mapped_memories(tmp_path):
    root = _make_tree(tmp_path)
    report = import_claude_memory(_store(tmp_path), root, apply=True)

    assert (report.new, report.updated, report.already, report.failed) == (3, 0, 0, 0)
    entries = {e.title: e for e in list_memory(_store(tmp_path), limit=10)}
    rule = entries["rule-one"]
    assert rule.kind == "lesson"
    assert rule.author == "claude-import"
    assert rule.scope_tool == "claude"
    assert rule.status == "confirmed"
    assert rule.body == "Short description\n\nThe rule body."
    assert entries["state-two"].kind == "fact"
    assert entries["pointer"].kind == "link"
    assert {"feedback", "claude-memory"} <= set(rule.tags)


def test_scope_project_only_when_path_exists(tmp_path):
    real_project = tmp_path / "work" / "app"
    real_project.mkdir(parents=True)
    root = tmp_path / "claude-projects"
    _memory_file(root, str(real_project).replace("/", "-"), "scoped")
    _memory_file(root, "-does-not-exist-anywhere", "unscoped")

    import_claude_memory(_store(tmp_path), root, apply=True)

    by_title = {e.title: e for e in list_memory(_store(tmp_path), limit=10)}
    assert by_title["scoped"].scope_project == str(real_project)
    assert by_title["unscoped"].scope_project is None


def test_second_apply_writes_nothing(tmp_path):
    root = _make_tree(tmp_path)
    import_claude_memory(_store(tmp_path), root, apply=True)
    before = len(list_memory(_store(tmp_path), limit=50, status=None))

    report = import_claude_memory(_store(tmp_path), root, apply=True)

    assert (report.new, report.updated, report.already) == (0, 0, 3)
    assert len(list_memory(_store(tmp_path), limit=50, status=None)) == before


def test_edited_file_supersedes_previous_import(tmp_path):
    root = _make_tree(tmp_path)
    import_claude_memory(_store(tmp_path), root, apply=True)
    old = next(e for e in list_memory(_store(tmp_path), limit=10) if e.title == "rule-one")

    _memory_file(root, "-some-project", "rule-one", body="The rule body, now revised.")
    report = import_claude_memory(_store(tmp_path), root, apply=True)

    assert (report.new, report.updated, report.already) == (0, 1, 2)
    assert not report.notes
    replaced = get_memory(_store(tmp_path), old.id)
    assert replaced is not None and replaced.superseded_by is not None
    new = get_memory(_store(tmp_path), replaced.superseded_by)
    assert new is not None and "revised" in new.body
    assert new.scope_project == old.scope_project


def test_secret_in_body_is_redacted(tmp_path):
    root = tmp_path / "claude-projects"
    _memory_file(root, "-leaky", "keys", body=f"Use this key: {AWS_KEY}")

    report = import_claude_memory(_store(tmp_path), root, apply=True)

    assert report.redactions >= 1
    (entry,) = list_memory(_store(tmp_path), limit=10)
    assert AWS_KEY not in entry.body
    assert "rule body" not in entry.body


def test_memory_index_is_skipped(tmp_path):
    root = tmp_path / "claude-projects"
    folder = root / "-only" / "memory"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text(
        "---\nname: index\ndescription: not a memory\n---\n\n- [a](a.md)\n", encoding="utf-8"
    )

    report = import_claude_memory(_store(tmp_path), root, apply=True)

    assert report.skipped == {"MEMORY.md index": 1}
    assert report.new == 0
    assert list_memory(_store(tmp_path), limit=10, status=None) == []


def test_files_without_frontmatter_or_body_are_skipped(tmp_path):
    root = tmp_path / "claude-projects"
    folder = root / "-mix" / "memory"
    folder.mkdir(parents=True)
    (folder / "plain.md").write_text("just text, no frontmatter\n", encoding="utf-8")
    (folder / "empty.md").write_text("---\nname: empty\n---\n\n", encoding="utf-8")

    report = import_claude_memory(_store(tmp_path), root, apply=False)

    assert report.skipped == {"no frontmatter": 1, "empty body": 1}
    assert report.new == 0


def test_unknown_type_becomes_candidate(tmp_path):
    root = tmp_path / "claude-projects"
    _memory_file(root, "-odd", "incident-note", mtype="incident")

    import_claude_memory(_store(tmp_path), root, apply=True)

    (entry,) = list_memory(_store(tmp_path), limit=10, status=None)
    assert entry.status == "candidate"
    assert entry.kind == "note"
    assert list_memory(_store(tmp_path), limit=10) == []


def test_missing_root_is_an_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_args(tmp_path, tmp_path / "nope", apply=False))
    assert exc.value.code == 1
    assert "no Claude projects directory" in capsys.readouterr().err
