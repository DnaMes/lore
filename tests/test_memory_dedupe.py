"""Exact memory deduplication and import prevention (AB#1606)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import lore.storage.embeddings as embeddings
import lore_cli
from lore.services.claude_memory_import import import_claude_memory
from lore.storage import add_memory, get_memory, list_memory, record_feedback, set_memory_status
from lore.storage.writer import v2_db_path


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "embed_text", lambda _text: None)


def _run_cli(store: Path, *arguments: str) -> None:
    args = lore_cli.build_parser().parse_args(
        ["--output-dir", str(store), "memory", "dedupe", *arguments]
    )
    lore_cli.cmd_memory(args)


def _set_updated(store: Path, memory_id: int, updated: str) -> None:
    with sqlite3.connect(v2_db_path(store)) as conn:
        conn.execute("UPDATE memory SET updated = ? WHERE id = ?", (updated, memory_id))


def _import_file(root: Path, slug: str = "-project", *, body: str = 'Use "care".') -> Path:
    folder = root / slug / "memory"
    folder.mkdir(parents=True)
    path = folder / "risk.md"
    path.write_text(f"---\nname: Risk Posture\ntype: feedback\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_dry_run_reports_normalized_duplicates_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    older = add_memory(tmp_path, "fact", "Risk  Posture", r"Use \"care\".")
    keeper = add_memory(
        tmp_path, "lesson", "risk\nposture", 'Use  "care".\n', scope_project="/work/project"
    )
    other_body = add_memory(tmp_path, "fact", "risk posture", 'Use "Care".')
    other_title = add_memory(tmp_path, "fact", "other title", 'Use "care".')
    before = v2_db_path(tmp_path).read_bytes()

    _run_cli(tmp_path)

    out = capsys.readouterr().out
    assert "dry run" in out
    assert f"keep #{keeper}" in out
    assert f"reject #{older}" in out
    assert "1 duplicate groups" in out and "Would reject: 1" in out
    assert v2_db_path(tmp_path).read_bytes() == before
    assert {entry.id for entry in list_memory(tmp_path)} == {older, keeper, other_body, other_title}


@pytest.mark.parametrize("winner", ["feedback", "scope", "updated"])
def test_keeper_priority(tmp_path: Path, winner: str, capsys: pytest.CaptureFixture[str]) -> None:
    feedback = add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work")
    scoped = add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work/deep/project")
    newest = add_memory(tmp_path, "fact", "Rule", "Body")
    for memory_id in (feedback, scoped):
        _set_updated(tmp_path, memory_id, "2026-10-17T00:30:00+02:00")
    _set_updated(tmp_path, newest, "2026-10-16T23:00:00+00:00")
    if winner == "feedback":
        record_feedback(tmp_path, feedback, "tester", 5)
    elif winner == "updated":
        # Equal path length makes the chronological timestamp the deciding factor.
        with sqlite3.connect(v2_db_path(tmp_path)) as conn:
            conn.execute("UPDATE memory SET scope_project = NULL")

    _run_cli(tmp_path, "--apply")

    expected = {"feedback": feedback, "scope": scoped, "updated": newest}[winner]
    assert [entry.id for entry in list_memory(tmp_path)] == [expected]
    assert f"keep #{expected}" in capsys.readouterr().out


def test_apply_keeps_rows_and_is_idempotent_and_reversible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [add_memory(tmp_path, "fact", "Rule", "Body") for _ in range(3)]

    _run_cli(tmp_path, "--apply")

    assert "Rejected: 2" in capsys.readouterr().out
    entries = list_memory(tmp_path, status=None)
    assert len(entries) == 3
    assert sorted(entry.status for entry in entries) == ["confirmed", "rejected", "rejected"]
    assert all(entry.body == "Body" and entry.superseded_by is None for entry in entries)
    _run_cli(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert "0 duplicate groups" in out and "Rejected: 0" in out
    rejected = next(entry.id for entry in entries if entry.status == "rejected")
    assert set_memory_status(tmp_path, rejected, "confirmed", from_status="rejected")
    assert len(list_memory(tmp_path)) == 2
    assert {entry.id for entry in list_memory(tmp_path, status=None)} == set(ids)


def test_excludes_candidates_rejected_and_superseded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = add_memory(tmp_path, "fact", "Rule", "Body")
    active = add_memory(tmp_path, "fact", "Rule", "Body", supersedes=old)
    candidate = add_memory(tmp_path, "fact", "Rule", "Body", status="candidate")
    rejected = add_memory(tmp_path, "fact", "Rule", "Body", status="candidate")
    set_memory_status(tmp_path, rejected, "rejected")

    _run_cli(tmp_path, "--apply")

    assert "Rejected: 0" in capsys.readouterr().out
    assert [entry.id for entry in list_memory(tmp_path)] == [active]
    assert get_memory(tmp_path, old).superseded_by == active
    assert get_memory(tmp_path, candidate).status == "candidate"
    assert get_memory(tmp_path, rejected).status == "rejected"


def test_missing_store_dry_run_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "missing"
    _run_cli(store)
    assert "Would reject: 0" in capsys.readouterr().out
    assert not store.exists()


@pytest.mark.parametrize("apply", [False, True])
def test_import_skips_existing_normalized_content_across_scopes(
    tmp_path: Path, apply: bool
) -> None:
    store = tmp_path / "store"
    existing = add_memory(store, "fact", "risk   posture", r"Use \"care\".", scope_project="/old")
    root = tmp_path / "claude"
    _import_file(root)

    report = import_claude_memory(store, root, apply=apply)

    assert report.duplicates == 1
    assert (report.new, report.updated, report.failed) == (0, 0, 0)
    assert [entry.id for entry in list_memory(store, status=None)] == [existing]


@pytest.mark.parametrize("apply", [False, True])
def test_import_deduplicates_files_in_one_run(tmp_path: Path, apply: bool) -> None:
    root = tmp_path / "claude"
    _import_file(root, "-one")
    _import_file(root, "-two", body='Use  "care".\n')
    store = tmp_path / "store"

    report = import_claude_memory(store, root, apply=apply)

    assert (report.new, report.duplicates) == (1, 1)
    if apply:
        assert len(list_memory(store, status=None)) == 1
    else:
        assert not store.exists()


@pytest.mark.parametrize("status", ["candidate", "rejected", "superseded"])
def test_import_does_not_treat_inactive_content_as_duplicate(tmp_path: Path, status: str) -> None:
    store = tmp_path / "store"
    previous = add_memory(
        store,
        "fact",
        "Risk Posture",
        'Use "care".',
        status="confirmed" if status == "superseded" else "candidate",
    )
    if status == "rejected":
        set_memory_status(store, previous, "rejected")
    elif status == "superseded":
        add_memory(store, "fact", "Risk Posture", "New body", supersedes=previous)
    root = tmp_path / "claude"
    _import_file(root)

    report = import_claude_memory(store, root, apply=True)

    assert (report.new, report.duplicates) == (1, 0)
    assert any(entry.body == 'Use "care".' for entry in list_memory(store))


def test_import_cli_reports_duplicates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = tmp_path / "store"
    add_memory(store, "fact", "Risk Posture", 'Use "care".')
    root = tmp_path / "claude"
    _import_file(root)
    args = lore_cli.build_parser().parse_args(
        ["--output-dir", str(store), "memory", "import-claude", "--root", str(root)]
    )

    lore_cli.cmd_memory(args)

    assert "1 duplicates" in capsys.readouterr().out


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("retained_copy", [False, True])
def test_import_tracks_content_superseded_during_the_run(
    tmp_path: Path, apply: bool, retained_copy: bool
) -> None:
    root = tmp_path / "claude"
    source = _import_file(root, "-a")
    store = tmp_path / "store"
    import_claude_memory(store, root, apply=True)
    if retained_copy:
        add_memory(store, "fact", "Risk Posture", 'Use "care".', scope_project="/legacy")
    source.write_text(source.read_text().replace('Use "care".', "Updated rule."))
    _import_file(root, "-z")

    report = import_claude_memory(store, root, apply=apply)

    assert report.updated == 1
    assert report.new == (0 if retained_copy else 1)
    assert report.duplicates == (1 if retained_copy else 0)


def test_changed_import_matching_other_memory_still_supersedes_and_refreshes_sha(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    root = tmp_path / "claude"
    path = _import_file(root, body="first body")
    import_claude_memory(store, root, apply=True)
    add_memory(store, "fact", "Risk Posture", "second body", scope_project="/elsewhere")
    path.write_text(
        "---\nname: Risk Posture\ntype: feedback\n---\n\nsecond body\n", encoding="utf-8"
    )

    first = import_claude_memory(store, root, apply=True)
    second = import_claude_memory(store, root, apply=True)

    assert (first.updated, first.duplicates) == (1, 0)
    assert (second.already, second.updated, second.duplicates) == (1, 0, 0)
