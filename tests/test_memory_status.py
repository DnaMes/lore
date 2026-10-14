"""Memory status (#197, migration 16): candidate / confirmed / rejected visibility."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace

import pytest

import lore.storage.embeddings as emb
import lore_cli
from lore.interfaces import mcp, web, web_data
from lore.storage import (
    add_memory,
    feedback_weight,
    get_memory,
    list_memory,
    recall_memory,
    record_feedback,
)
from lore.storage.memory import set_memory_status
from lore.storage.schema import MIGRATIONS, current_version, initialise, open_connection
from lore.storage.writer import v2_db_path


@pytest.fixture(autouse=True)
def _no_embedding_backend(monkeypatch) -> None:
    """Keep these tests off the real model; one test installs its own fake."""
    monkeypatch.setattr(emb, "embed_text", lambda _t: None)
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)


def _ids(entries) -> list[int]:
    return [e.id for e in entries]


def _three(tmp_path) -> tuple[int, int, int]:
    """One confirmed, one candidate, one rejected memory, all matching 'alpha'."""
    confirmed = add_memory(tmp_path, "note", "confirmed one", "alpha beta")
    candidate = add_memory(tmp_path, "note", "candidate one", "alpha beta", status="candidate")
    rejected = add_memory(tmp_path, "note", "rejected one", "alpha beta", status="candidate")
    assert set_memory_status(tmp_path, rejected, "rejected") is True
    return confirmed, candidate, rejected


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_16_from_v15_keeps_rows_confirmed(tmp_path):
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 15]
    try:
        conn = initialise(v2_db_path(tmp_path))
    finally:
        MIGRATIONS[:] = full
    try:
        assert current_version(conn) == 15
        assert "status" not in {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        cur = conn.execute(
            "INSERT INTO memory (kind, title, body, author, created, updated) "
            "VALUES ('note', 'keep me', 'alpha', 'human', 't', 't')"
        )
        memory_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO search_index (entity_type, entity_id, tool, project, title, body) "
            "VALUES ('memory', ?, '', '', 'keep me', 'alpha')",
            (str(memory_id),),
        )
    finally:
        conn.close()

    entry = get_memory(tmp_path, memory_id)  # opens and migrates
    assert entry is not None
    assert entry.status == "confirmed"
    assert entry.to_dict()["status"] == "confirmed"

    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) >= 16
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(memory)")}
        assert "idx_memory_status" in indexes
    finally:
        conn.close()
    assert _ids(list_memory(tmp_path)) == [memory_id]
    assert _ids(recall_memory(tmp_path, "alpha", semantic=False)) == [memory_id]


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_default_hides_candidate_and_rejected(tmp_path):
    confirmed, _candidate, _rejected = _three(tmp_path)
    assert _ids(list_memory(tmp_path)) == [confirmed]
    assert _ids(recall_memory(tmp_path, "alpha", semantic=False)) == [confirmed]
    assert _ids(recall_memory(tmp_path, "")) == [confirmed]


def test_status_none_shows_all_and_value_filters(tmp_path):
    confirmed, candidate, rejected = _three(tmp_path)
    assert set(_ids(list_memory(tmp_path, status=None))) == {confirmed, candidate, rejected}
    assert set(_ids(recall_memory(tmp_path, "alpha", semantic=False, status=None))) == {
        confirmed,
        candidate,
        rejected,
    }
    assert _ids(list_memory(tmp_path, status="candidate")) == [candidate]
    assert _ids(recall_memory(tmp_path, "alpha", semantic=False, status="candidate")) == [candidate]
    assert _ids(recall_memory(tmp_path, "", status="rejected")) == [rejected]


def test_get_memory_returns_any_status(tmp_path):
    _confirmed, candidate, rejected = _three(tmp_path)
    for memory_id, status in ((candidate, "candidate"), (rejected, "rejected")):
        entry = get_memory(tmp_path, memory_id)
        assert entry is not None and entry.status == status


def test_fts_overfetch_is_not_crowded_out_by_candidates(tmp_path):
    for i in range(12):
        add_memory(tmp_path, "note", f"candidate {i}", "alpha alpha alpha", status="candidate")
    confirmed = add_memory(tmp_path, "note", "the confirmed one", "alpha " + "filler " * 40)
    hits = recall_memory(tmp_path, "alpha", semantic=False, limit=1)
    assert _ids(hits) == [confirmed]


def test_semantic_and_hybrid_paths_hide_candidates(tmp_path, monkeypatch):
    def fake(text: str):
        return [1.0, 0.0] if "postgres" in text.lower() else [0.0, 1.0]

    monkeypatch.setattr(emb, "embed_text", fake)
    monkeypatch.setattr(emb, "embeddings_available", lambda: True)
    monkeypatch.delenv("LORE_MEMORY_RECALL_MIN_SIMILARITY", raising=False)
    confirmed = add_memory(tmp_path, "fact", "Postgres notes", "relational")
    add_memory(tmp_path, "fact", "Postgres draft", "relational", status="candidate")
    assert _ids(recall_memory(tmp_path, "postgres")) == [confirmed]  # hybrid
    assert _ids(recall_memory(tmp_path, "postgres", semantic=True)) == [confirmed]
    assert len(recall_memory(tmp_path, "postgres", semantic=True, status=None)) == 2


# ---------------------------------------------------------------------------
# Writes and transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["rejected", "bogus", ""])
def test_add_memory_rejects_other_statuses(tmp_path, status):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "note", "t", "b", status=status)
    assert list_memory(tmp_path, status=None) == []


def test_candidate_transitions(tmp_path):
    first = add_memory(tmp_path, "note", "a", "alpha", status="candidate")
    assert set_memory_status(tmp_path, first, "confirmed") is True
    entry = get_memory(tmp_path, first)
    assert entry is not None and entry.status == "confirmed"

    second = add_memory(tmp_path, "note", "b", "alpha", status="candidate")
    assert set_memory_status(tmp_path, second, "rejected") is True
    entry = get_memory(tmp_path, second)  # rejected rows are kept
    assert entry is not None and entry.status == "rejected"


def test_disallowed_transitions_raise_and_change_nothing(tmp_path):
    confirmed, _candidate, rejected = _three(tmp_path)
    for memory_id in (confirmed, rejected):
        for target in ("confirmed", "rejected", "candidate"):
            with pytest.raises(ValueError):
                set_memory_status(tmp_path, memory_id, target)
    candidate = add_memory(tmp_path, "note", "c", "alpha", status="candidate")
    for bad in ("candidate", "bogus", ""):
        with pytest.raises(ValueError):
            set_memory_status(tmp_path, candidate, bad)
    entry = get_memory(tmp_path, candidate)
    assert entry is not None and entry.status == "candidate"
    after = get_memory(tmp_path, confirmed)
    assert after is not None and after.status == "confirmed"


def test_set_status_unknown_id_is_false(tmp_path):
    assert set_memory_status(tmp_path, 4242, "confirmed") is False


# ---------------------------------------------------------------------------
# Surfaces: a candidate stays hidden everywhere a user or agent reads memory
# ---------------------------------------------------------------------------


def _candidate_and_confirmed(tmp_path) -> None:
    add_memory(tmp_path, "note", "Confirmed zebra", "zebra stripes")
    add_memory(tmp_path, "note", "Candidate zebra", "zebra stripes", status="candidate")


def test_mcp_recall_hides_candidates(tmp_path):
    _candidate_and_confirmed(tmp_path)
    server = mcp.create_server()
    server.output_dir = tmp_path
    handler = server.tools["memory_recall"]["handler"]
    for query in ("zebra", ""):
        payload = json.loads(asyncio.run(handler({"query": query})))
        assert [m["title"] for m in payload["memories"]] == ["Confirmed zebra"]


def test_web_memory_page_hides_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    _candidate_and_confirmed(tmp_path)
    with web.app.test_client() as client:
        for url in ("/memory", "/memory?q=zebra"):
            body = client.get(url).get_data(as_text=True)
            assert "Confirmed zebra" in body
            assert "Candidate zebra" not in body


@pytest.mark.parametrize("action", ["list", "search"])
def test_cli_list_and_search_hide_candidates(tmp_path, capsys, action):
    _candidate_and_confirmed(tmp_path)
    args = Namespace(
        output_dir=str(tmp_path),
        memory_action=action,
        query="zebra",
        kind=None,
        project=None,
        all=False,
        limit=10,
        semantic=False,
    )
    lore_cli.cmd_memory(args)
    out = capsys.readouterr().out
    assert "Confirmed zebra" in out
    assert "Candidate zebra" not in out


def test_rating_a_candidate_is_rejected(tmp_path):
    candidate = add_memory(tmp_path, "note", "Draft", "alpha", status="candidate")
    with pytest.raises(ValueError, match="not confirmed"):
        record_feedback(tmp_path, candidate, "agent", 5)
    assert feedback_weight(tmp_path, candidate) == (0.5, 0)
    assert list_memory(tmp_path) == []
