"""Exact memory deduplication and import prevention (AB#1606)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import lore.storage.embeddings as embeddings
import lore_cli
from lore.services.claude_memory_import import import_claude_memory
from lore.services.memory_context import select_context_memories
from lore.services.memory_dedupe import dedupe_memories
from lore.storage import add_memory, get_memory, list_memory, record_feedback, set_memory_status
from lore.storage.memory import reject_memories_bulk, restore_rejected_memories_bulk
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
    # Rewritten for scope-aware identity: the grouped pair must share kind and scope
    # (all global "fact"); a project-scoped copy of the same content is a separate
    # memory and must survive, since the old test merged across scopes/kinds.
    older = add_memory(tmp_path, "fact", "Risk  Posture", r"Use \"care\".")
    keeper = add_memory(tmp_path, "fact", "risk\nposture", 'Use  "care".\n')
    other_body = add_memory(tmp_path, "fact", "risk posture", 'Use "Care".')
    other_title = add_memory(tmp_path, "fact", "other title", 'Use "care".')
    scoped_copy = add_memory(
        tmp_path, "fact", "risk posture", r"Use \"care\".", scope_project="/work/project"
    )
    before = v2_db_path(tmp_path).read_bytes()

    _run_cli(tmp_path)

    out = capsys.readouterr().out
    assert "dry run" in out
    assert f"keep #{keeper}" in out
    assert f"reject #{older}" in out
    assert "1 duplicate groups" in out and "Would reject: 1" in out
    assert v2_db_path(tmp_path).read_bytes() == before
    assert {entry.id for entry in list_memory(tmp_path)} == {
        older,
        keeper,
        other_body,
        other_title,
        scoped_copy,
    }


def test_global_and_identical_project_copy_both_survive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A global memory and an identical project-scoped copy are never merged."""
    global_id = add_memory(tmp_path, "fact", "Risk Posture", 'Use "care".')
    scoped_id = add_memory(
        tmp_path, "fact", "Risk Posture", 'Use "care".', scope_project="/work/project"
    )

    _run_cli(tmp_path, "--apply")

    out = capsys.readouterr().out
    assert "0 duplicate groups" in out and "Rejected: 0" in out
    assert {entry.id for entry in list_memory(tmp_path)} == {global_id, scoped_id}


def test_identical_copies_in_same_scope_are_still_deduped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = add_memory(tmp_path, "fact", "Risk Posture", 'Use "care".', scope_project="/work")
    second = add_memory(tmp_path, "fact", "Risk Posture", 'Use "care".', scope_project="/work")
    global_id = add_memory(tmp_path, "fact", "Risk Posture", 'Use "care".')

    _run_cli(tmp_path, "--apply")

    out = capsys.readouterr().out
    assert "1 duplicate groups" in out and "Rejected: 1" in out
    survivor = {entry.id for entry in list_memory(tmp_path)}
    assert global_id in survivor and len(survivor) == 2
    rejected_id = next(memory_id for memory_id in (first, second) if memory_id not in survivor)
    assert get_memory(tmp_path, rejected_id).status == "rejected"


@pytest.mark.parametrize("winner", ["feedback", "updated"])
def test_keeper_priority(tmp_path: Path, winner: str, capsys: pytest.CaptureFixture[str]) -> None:
    # Rewritten for scope-aware identity: the "scope" case merged cross-scope copies,
    # which is now forbidden, and within one scope the path-length tie-break is inert
    # (every grouped entry shares the same scope_project), so it was removed.
    feedback = add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work")
    newest = add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work")
    _set_updated(tmp_path, feedback, "2026-10-17T00:30:00+02:00")
    _set_updated(tmp_path, newest, "2026-10-16T23:00:00+00:00")
    if winner == "feedback":
        record_feedback(tmp_path, feedback, "tester", 5)

    _run_cli(tmp_path, "--apply")

    expected = feedback if winner == "feedback" else newest
    assert [entry.id for entry in list_memory(tmp_path)] == [expected]
    assert f"keep #{expected}" in capsys.readouterr().out


def test_apply_keeps_rows_and_is_idempotent_and_reversible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [add_memory(tmp_path, "fact", "Rule", "Body") for _ in range(3)]

    _run_cli(tmp_path, "--apply")

    out = capsys.readouterr().out
    assert "Rejected: 2" in out
    undo_line = next(line for line in out.splitlines() if line.startswith("Undo log:"))
    undo_path = Path(undo_line.split(":", 1)[1].strip())
    assert undo_path.is_file()
    entries = list_memory(tmp_path, status=None)
    assert len(entries) == 3
    assert sorted(entry.status for entry in entries) == ["confirmed", "rejected", "rejected"]
    assert all(entry.body == "Body" and entry.superseded_by is None for entry in entries)
    _run_cli(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert "0 duplicate groups" in out and "Rejected: 0" in out
    _run_cli(tmp_path, "--restore", str(undo_path))
    assert "Restored: 2" in capsys.readouterr().out
    assert len(list_memory(tmp_path)) == 3
    assert {entry.id for entry in list_memory(tmp_path, status=None)} == set(ids)
    _run_cli(tmp_path, "--restore", str(undo_path))
    assert "Restored: 0" in capsys.readouterr().out


def test_restore_refuses_undo_log_from_another_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A log fingerprinted for one store must not change a different store."""
    store_a = tmp_path / "store-a"
    store_b = tmp_path / "store-b"
    add_memory(store_a, "fact", "Rule", "Body")
    add_memory(store_a, "fact", "Rule", "Body")
    _, undo_path = dedupe_memories(store_a, apply=True)
    assert undo_path is not None
    victim_b = add_memory(store_b, "fact", "Rule", "Body")
    add_memory(store_b, "fact", "Rule", "Body")
    before_b = v2_db_path(store_b).read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        _run_cli(store_b, "--restore", str(undo_path))

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "refusing to restore into a different store" in err
    assert "belongs to store" in err
    assert v2_db_path(store_b).read_bytes() == before_b  # nothing changed
    assert get_memory(store_b, victim_b).status == "confirmed"


def test_legacy_undo_log_without_store_identity_restores_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Logs written before store fingerprints existed still restore, with a warning."""
    add_memory(tmp_path, "fact", "Rule", "Body")
    add_memory(tmp_path, "fact", "Rule", "Body")
    groups, undo_path = dedupe_memories(tmp_path, apply=True)
    assert undo_path is not None
    rejected_id = groups[0].rejected_ids[0]
    legacy = tmp_path / "legacy-undo.json"
    legacy_payload = json.loads(undo_path.read_text(encoding="utf-8"))
    del legacy_payload["store"]
    legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")

    _run_cli(tmp_path, "--restore", str(legacy))

    captured = capsys.readouterr()
    assert "has no store identity" in captured.err and "Warning" in captured.err
    assert "Restored: 1" in captured.out
    assert get_memory(tmp_path, rejected_id).status == "confirmed"


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


def test_reject_memories_bulk_is_atomic(tmp_path: Path) -> None:
    first = add_memory(tmp_path, "fact", "A", "one")
    second = add_memory(tmp_path, "fact", "B", "two")
    third = add_memory(tmp_path, "fact", "C", "three")
    set_memory_status(tmp_path, third, "rejected", from_status="confirmed")

    with pytest.raises(ValueError, match="not active confirmed"):
        reject_memories_bulk(tmp_path, [first, second, third])

    assert {entry.id for entry in list_memory(tmp_path)} == {first, second}


def test_restore_rejected_memories_bulk_counts(tmp_path: Path) -> None:
    add_memory(tmp_path, "fact", "Rule", "Body")
    add_memory(tmp_path, "fact", "Rule", "Body")
    groups, undo_path = dedupe_memories(tmp_path, apply=True)
    assert undo_path is not None
    rejected_id = groups[0].rejected_ids[0]
    kept_id = groups[0].kept_id

    restored, skipped, missing = restore_rejected_memories_bulk(
        tmp_path, [rejected_id, kept_id, 999_999]
    )
    assert (restored, skipped, missing) == (1, 1, 1)
    assert get_memory(tmp_path, rejected_id).status == "confirmed"


def test_set_memory_status_rejects_invalid_from_status(tmp_path: Path) -> None:
    memory_id = add_memory(tmp_path, "fact", "Rule", "Body")
    with pytest.raises(ValueError, match="expected one of"):
        set_memory_status(tmp_path, memory_id, "rejected", from_status="superseded")


def test_keeper_priority_mixed_naive_and_aware_updated(tmp_path: Path) -> None:
    older = add_memory(tmp_path, "fact", "Rule", "Body")
    newer = add_memory(tmp_path, "fact", "Rule", "Body")
    _set_updated(tmp_path, older, "2026-10-17T10:00:00")
    _set_updated(tmp_path, newer, "2026-10-17T11:00:00+00:00")

    groups, _ = dedupe_memories(tmp_path, apply=True)

    assert len(groups) == 1
    assert groups[0].kept_id == newer
    assert older in groups[0].rejected_ids


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


def _duplicate_pair(store: Path) -> tuple[int, int]:
    first = add_memory(store, "fact", "dup", "same body")
    second = add_memory(store, "fact", "dup", "same body")
    return first, second


def test_two_applies_never_share_an_undo_log(tmp_path: Path) -> None:
    from lore.services.memory_dedupe import _write_undo_log

    groups, _ = dedupe_memories(tmp_path)
    assert groups == []
    first = _write_undo_log(tmp_path, groups)
    second = _write_undo_log(tmp_path, groups)
    assert first != second and first.exists() and second.exists()


def test_failed_apply_leaves_no_undo_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lore.services.memory_dedupe as service

    _duplicate_pair(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> int:
        raise ValueError("concurrent change")

    monkeypatch.setattr(service, "reject_memories_bulk", boom)
    with pytest.raises(ValueError):
        dedupe_memories(tmp_path, apply=True)
    assert list(tmp_path.glob("dedupe-undo-*.json")) == []


def test_restore_skips_superseded_rows(tmp_path: Path) -> None:
    old = add_memory(tmp_path, "fact", "topic", "old body")
    new = add_memory(tmp_path, "fact", "topic", "new body", supersedes=old)
    set_memory_status(tmp_path, new, "rejected", from_status="confirmed")
    with sqlite3.connect(v2_db_path(tmp_path)) as conn:
        conn.execute("UPDATE memory SET status = 'rejected' WHERE id = ?", (old,))
    assert restore_rejected_memories_bulk(tmp_path, [old, new]) == (1, 1, 0)
    assert get_memory(tmp_path, old).status == "rejected"
    assert get_memory(tmp_path, new).status == "confirmed"


def test_malformed_undo_log_is_a_value_error(tmp_path: Path) -> None:
    from lore.services.memory_dedupe import restore_dedupe_undo

    for text in ('{"groups": 5}', '{"groups": [{"rejected_ids": "x"}]}', "{}", "[]"):
        log = tmp_path / "undo.json"
        log.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="malformed"):
            restore_dedupe_undo(tmp_path, log)


def test_list_superseded_by_queries_predecessors_directly(tmp_path: Path) -> None:
    from lore.storage.memory import list_superseded_by

    old = add_memory(tmp_path, "fact", "topic", "old body")
    new = add_memory(tmp_path, "fact", "topic", "new body", supersedes=old)
    assert list_superseded_by(tmp_path, new) == [old]
    assert list_superseded_by(tmp_path, old) == []


def test_scope_project_trailing_slash_is_grouped(tmp_path: Path) -> None:
    """The recall normalizer resolves ``/work/repo/`` and ``/work/repo`` together."""
    first = add_memory(tmp_path, "fact", "Shared rule", "Body", scope_project="/work/repo/")
    second = add_memory(tmp_path, "fact", "Shared rule", "Body", scope_project="/work/repo")

    groups, _ = dedupe_memories(tmp_path)

    assert len(groups) == 1
    assert sorted(groups[0].rejected_ids + [groups[0].kept_id]) == sorted([first, second])


def test_scope_project_whitespace_in_path_is_not_collapsed(tmp_path: Path) -> None:
    """``/work/repo/a  b`` and ``/work/repo/a b`` are distinct scopes."""
    add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work/repo/a  b")
    add_memory(tmp_path, "fact", "Rule", "Body", scope_project="/work/repo/a b")

    groups, _ = dedupe_memories(tmp_path)

    assert groups == []


def test_dedupe_groups_are_consistent_with_context_recall(tmp_path: Path) -> None:
    """A rejected duplicate may be recalled for its project only if the keeper is too."""
    scenarios = [
        (None, None),
        ("/work/repo/", None),
        ("/other", "tool-a"),
        ("/work/repo/sub/../sub", None),
    ]
    expected_groups = 0
    for scope_project, scope_tool in scenarios:
        add_memory(
            tmp_path,
            "fact",
            f"Rule {scope_project} {scope_tool}",
            "Same body",
            scope_project=scope_project,
            scope_tool=scope_tool,
        )
        second = add_memory(
            tmp_path,
            "fact",
            f"Rule {scope_project} {scope_tool}",
            "Same body",
            scope_project=scope_project,
            scope_tool=scope_tool,
        )
        record_feedback(tmp_path, second, "tester", 5)
        expected_groups += 1

    groups, _ = dedupe_memories(tmp_path)
    assert len(groups) == expected_groups

    for group in groups:
        keeper = get_memory(tmp_path, group.kept_id)
        selected = {
            entry.id
            for entry in select_context_memories(
                tmp_path,
                scope_project=keeper.scope_project,
                scope_tool=keeper.scope_tool,
            )
        }
        assert keeper.id in selected
        for rejected_id in group.rejected_ids:
            if rejected_id in selected:
                assert keeper.id in selected
