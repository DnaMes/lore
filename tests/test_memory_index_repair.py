"""Repair command for the memory search rows (#159).

The memory table and the shared FTS index are written in one transaction, so a
crash or an interrupted migration can still leave a memory that recall cannot
find, or a search row whose memory is gone. This issue's remaining acceptance
criterion is the repair command that detects and reconstructs the first and drops
the second.
"""

from __future__ import annotations

import json

import pytest

from lore.storage import (
    add_memory,
    memory_index_health,
    recall_memory,
    repair_memory_index,
)
from lore.storage.memory import _connect
from lore.storage.writer import v2_db_path


@pytest.fixture()
def store(tmp_path):
    """Three memories with intact search rows."""
    for index in range(3):
        add_memory(
            tmp_path,
            kind="fact",
            title=f"Fakt {index}",
            body=f"The build needs kwblossom step {index}",
            scope_project="/repo/lore",
        )
    return tmp_path


def _drop_search_rows(store, memory_ids):
    conn = _connect(store)
    try:
        for memory_id in memory_ids:
            conn.execute(
                "DELETE FROM search_index WHERE entity_type = 'memory' AND entity_id = ?",
                (str(memory_id),),
            )
        conn.commit()
    finally:
        conn.close()


def test_healthy_store_reports_nothing_to_do(store):
    health = memory_index_health(store)
    assert health.total_memories == 3
    assert health.indexed == 3
    assert health.missing == []
    assert health.orphans == []
    assert health.healthy is True


def test_missing_rows_are_detected(store):
    _drop_search_rows(store, [1, 3])
    health = memory_index_health(store)
    assert health.missing == [1, 3]
    assert health.healthy is False


def test_dry_run_writes_nothing(store):
    _drop_search_rows(store, [2])
    repaired = repair_memory_index(store, apply=False)
    assert repaired.missing == [2]
    assert memory_index_health(store).missing == [2]


def _fts_entity_ids(store, term: str) -> set[str]:
    """Ids the shared FTS index itself returns (recall has its own fallbacks)."""
    conn = _connect(store)
    try:
        rows = conn.execute(
            "SELECT entity_id FROM search_index WHERE search_index MATCH ?",
            (f'"{term}"*',),
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def test_apply_rebuilds_the_rows_so_the_index_finds_the_memory_again(store):
    _drop_search_rows(store, [2])
    assert "2" not in _fts_entity_ids(store, "kwblossom")

    repaired = repair_memory_index(store, apply=True)
    assert repaired.healthy is True

    assert "2" in _fts_entity_ids(store, "kwblossom")
    # Recall keeps a keyword fallback, so it found the memory even before the
    # repair; the index row is what the ranking and snippets need.
    assert any(hit.id == 2 for hit in recall_memory(store, "kwblossom step 1", limit=5))
    assert memory_index_health(store).indexed == 3


def test_orphan_rows_are_removed(store):
    conn = _connect(store)
    try:
        conn.execute(
            "INSERT INTO search_index (entity_type, entity_id, tool, project, title, body) "
            "VALUES ('memory', '999', '', '', 'gone', 'gone body')"
        )
        conn.commit()
    finally:
        conn.close()

    health = memory_index_health(store)
    assert health.orphans == ["999"]

    repaired = repair_memory_index(store, apply=True)
    assert repaired.orphans == []
    assert repaired.healthy is True


def test_repair_keeps_the_scope_columns(store):
    _drop_search_rows(store, [1])
    repair_memory_index(store, apply=True)
    conn = _connect(store)
    try:
        row = conn.execute(
            "SELECT tool, project, title FROM search_index "
            "WHERE entity_type = 'memory' AND entity_id = '1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["project"] == "/repo/lore"
    assert row["title"] == "Fakt 0"


def test_repair_is_idempotent(store):
    _drop_search_rows(store, [1, 2, 3])
    repair_memory_index(store, apply=True)
    again = repair_memory_index(store, apply=True)
    assert again.healthy is True
    conn = _connect(store)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM search_index WHERE entity_type = 'memory'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert rows == 3


def test_repair_recovers_rows_after_a_store_rebuild(store):
    """A rebuilt store keeps the memories; the repair re-attaches the search rows."""
    from lore.storage import write_sessions_safe

    write_sessions_safe(store, [])
    _drop_search_rows(store, [1])
    repaired = repair_memory_index(store, apply=True)
    assert repaired.total_memories >= 3
    assert repaired.missing == []


def test_cli_reports_and_applies(store, capsys):
    import argparse

    import lore_cli

    _drop_search_rows(store, [3])
    args = argparse.Namespace(
        memory_action="repair-index",
        apply=False,
        json=True,
        output_dir=str(store),
    )
    lore_cli.cmd_memory(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing"] == [3]
    assert payload["applied"] is False

    args.apply = True
    lore_cli.cmd_memory(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing"] == []
    assert payload["healthy"] is True


def test_cli_text_output_explains_the_dry_run(store, capsys):
    import argparse

    import lore_cli

    _drop_search_rows(store, [2])
    lore_cli.cmd_memory(
        argparse.Namespace(
            memory_action="repair-index", apply=False, json=False, output_dir=str(store)
        )
    )
    out = capsys.readouterr().out
    assert "Missing search rows for 1 memories" in out
    assert "Dry run" in out


def test_missing_store_is_not_an_error(tmp_path):
    """A fresh home has no memory table yet; the health check must not raise."""
    from lore.storage.memory import memory_index_health as health_fn

    assert v2_db_path(tmp_path).exists() is False
    health = health_fn(tmp_path)
    assert health.total_memories == 0
    assert health.healthy is True
