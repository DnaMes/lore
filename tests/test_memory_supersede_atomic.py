"""Similar-memory candidates and atomic ``supersedes`` (#197)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from argparse import Namespace
from typing import List, Optional

import pytest

import lore.storage.embeddings as emb
import lore.storage.memory as memory_mod
import lore_cli
from lore.interfaces import mcp
from lore.storage import (
    add_memory,
    delete_memory,
    get_memory,
    list_memory,
    recall_memory,
    record_feedback,
    supersede_memory,
)
from lore.storage.memory import find_similar_memories, set_memory_status, similarity_threshold

TOPICS = (("postgres", "database"), ("sourdough", "bread"))


def _fake_embed(text: str) -> Optional[List[float]]:
    lowered = text.lower()
    for axis, words in enumerate(TOPICS):
        if any(w in lowered for w in words):
            return [1.0 if i == axis else 0.0 for i in range(len(TOPICS) + 1)]
    return [0.0] * len(TOPICS) + [1.0]


@pytest.fixture
def fake(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed_text", _fake_embed)
    monkeypatch.setattr(emb, "embeddings_available", lambda: True)
    monkeypatch.delenv("LORE_SIMILARITY_THRESHOLD", raising=False)


@pytest.fixture
def no_backend(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed_text", lambda _t: None)
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)


def _counts(tmp_path) -> tuple[int, int]:
    conn = memory_mod._connect(tmp_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        fts = conn.execute(
            "SELECT COUNT(*) FROM search_index WHERE entity_type = 'memory'"
        ).fetchone()[0]
        return int(rows), int(fts)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# find_similar_memories
# ---------------------------------------------------------------------------


def test_similar_finds_duplicates_sorted_with_limit(tmp_path, fake):
    a = add_memory(tmp_path, "fact", "Postgres A", "relational store")
    b = add_memory(tmp_path, "fact", "Postgres B", "database server")
    add_memory(tmp_path, "fact", "Sourdough", "bread baking")
    hits = find_similar_memories(tmp_path, "postgres tuning", scope_project=None)
    assert hits is not None
    assert {h["id"] for h in hits} == {a, b}
    assert all(set(h) == {"id", "title", "similarity"} for h in hits)
    assert hits[0]["similarity"] == pytest.approx(1.0)
    assert len(find_similar_memories(tmp_path, "postgres", scope_project=None, limit=1) or []) == 1


def test_similar_excludes_candidates_rejected_superseded_and_other_projects(tmp_path, fake):
    keep = add_memory(tmp_path, "fact", "Keep", "postgres", scope_project="p1")
    add_memory(tmp_path, "fact", "Candidate", "postgres", scope_project="p1", status="candidate")
    rejected = add_memory(
        tmp_path, "fact", "Rejected", "postgres", scope_project="p1", status="candidate"
    )
    set_memory_status(tmp_path, rejected, "rejected")
    add_memory(tmp_path, "fact", "Other project", "postgres", scope_project="p2")
    add_memory(tmp_path, "fact", "Global", "postgres")
    old = add_memory(tmp_path, "fact", "Old", "postgres", scope_project="p1")
    add_memory(tmp_path, "fact", "Newer", "postgres", scope_project="p1", supersedes=old)
    hits = find_similar_memories(tmp_path, "postgres", scope_project="p1")
    assert hits is not None
    titles = {h["title"] for h in hits}
    assert titles == {"Keep", "Newer"}
    assert keep in {h["id"] for h in hits}
    # None matches only global memories.
    globals_ = find_similar_memories(tmp_path, "postgres", scope_project=None) or []
    assert {h["title"] for h in globals_} == {"Global"}


def test_similar_does_not_contain_the_new_text_itself(tmp_path, fake):
    assert find_similar_memories(tmp_path, "postgres brand new", scope_project=None) == []


def test_similar_is_none_without_backend(tmp_path, no_backend):
    add_memory(tmp_path, "fact", "Postgres", "relational")
    assert find_similar_memories(tmp_path, "postgres", scope_project=None) is None


def test_similar_respects_threshold_env(tmp_path, fake, monkeypatch):
    add_memory(tmp_path, "fact", "Sourdough", "bread")
    # Orthogonal fake vectors: similarity is 0.0, so only a threshold of 0 matches.
    assert find_similar_memories(tmp_path, "postgres", scope_project=None) == []
    monkeypatch.setenv("LORE_SIMILARITY_THRESHOLD", "0")
    assert len(find_similar_memories(tmp_path, "postgres", scope_project=None) or []) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0.85), ("", 0.85), ("banana", 0.85), ("nan", 0.85), ("0.5", 0.5), ("9", 1.0)],
)
def test_threshold_env_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LORE_SIMILARITY_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("LORE_SIMILARITY_THRESHOLD", raw)
    assert similarity_threshold() == expected


# ---------------------------------------------------------------------------
# supersedes
# ---------------------------------------------------------------------------


def test_supersedes_links_old_and_default_recall_returns_new(tmp_path, fake):
    old = add_memory(tmp_path, "fact", "Postgres 15", "relational v15", scope_project="p")
    new = add_memory(
        tmp_path, "fact", "Postgres 16", "relational v16", scope_project="p", supersedes=old
    )
    entry = get_memory(tmp_path, old)
    assert entry is not None and entry.superseded_by == new
    assert [e.id for e in recall_memory(tmp_path, "postgres", scope_project="p")] == [new]
    assert [e.id for e in list_memory(tmp_path)] == [new]


def test_supersedes_allows_a_different_kind(tmp_path, fake):
    old = add_memory(tmp_path, "fact", "Postgres 15", "relational")
    new = add_memory(tmp_path, "decision", "Use Postgres 16", "relational", supersedes=old)
    entry = get_memory(tmp_path, new)
    assert entry is not None and entry.kind == "decision"


def _violations(tmp_path) -> dict:
    ok = add_memory(tmp_path, "fact", "Active", "x", scope_project="p")
    superseded = add_memory(tmp_path, "fact", "Superseded", "x", scope_project="p")
    add_memory(tmp_path, "fact", "Replacement", "x", scope_project="p", supersedes=superseded)
    candidate = add_memory(tmp_path, "fact", "Cand", "x", scope_project="p", status="candidate")
    rejected = add_memory(tmp_path, "fact", "Rej", "x", scope_project="p", status="candidate")
    set_memory_status(tmp_path, rejected, "rejected")
    return {
        "missing": 9999,
        "superseded": superseded,
        "candidate": candidate,
        "rejected": rejected,
        "other_project": ok,  # written below with a different scope
    }


@pytest.mark.parametrize(
    "case", ["missing", "superseded", "candidate", "rejected", "other_project"]
)
def test_supersedes_violation_writes_nothing(tmp_path, no_backend, case):
    targets = _violations(tmp_path)
    scope = "q" if case == "other_project" else "p"
    before = _counts(tmp_path)
    target_before = get_memory(tmp_path, targets[case])
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "New", "y", scope_project=scope, supersedes=targets[case])
    assert _counts(tmp_path) == before
    assert get_memory(tmp_path, targets[case]) == target_before


def test_candidate_cannot_supersede(tmp_path, no_backend):
    old = add_memory(tmp_path, "fact", "Old", "x")
    before = _counts(tmp_path)
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "New", "y", supersedes=old, status="candidate")
    assert _counts(tmp_path) == before


def test_supersedes_none_scope_must_match_none(tmp_path, no_backend):
    old = add_memory(tmp_path, "fact", "Old", "x", scope_project="p")
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "New", "y", supersedes=old)  # scope None != "p"


class _KillUpdate:
    """Connection proxy whose superseded_by UPDATE fails, after the INSERT ran."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args):
        if sql.strip().startswith("UPDATE memory SET superseded_by"):
            raise sqlite3.OperationalError("injected failure")
        return self._conn.execute(sql, *args)

    def close(self) -> None:
        self._conn.close()


def test_failure_after_insert_rolls_back_everything(tmp_path, no_backend, monkeypatch):
    old = add_memory(tmp_path, "fact", "Old", "x", tags=["t1"])
    before = _counts(tmp_path)
    old_before = get_memory(tmp_path, old)
    real_connect = memory_mod._connect
    monkeypatch.setattr(memory_mod, "_connect", lambda d: _KillUpdate(real_connect(d)))
    with pytest.raises(sqlite3.OperationalError):
        add_memory(tmp_path, "fact", "New", "y", tags=["t2"], supersedes=old)
    monkeypatch.setattr(memory_mod, "_connect", real_connect)

    assert _counts(tmp_path) == before
    conn = real_connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_tags").fetchone()[0] == 1
    finally:
        conn.close()
    assert get_memory(tmp_path, old) == old_before


def test_supersede_memory_still_inherits_and_links(tmp_path, no_backend):
    old = add_memory(tmp_path, "decision", "v1", "x", scope_project="/proj", tags=["t1"])
    new = supersede_memory(tmp_path, old, "v2", "y")
    entry = get_memory(tmp_path, new)
    assert entry is not None and entry.kind == "decision" and entry.tags == ["t1"]
    old_entry = get_memory(tmp_path, old)
    assert old_entry is not None and old_entry.superseded_by == new


# ---------------------------------------------------------------------------
# MCP memory_write
# ---------------------------------------------------------------------------


def _server(tmp_path):
    server = mcp.create_server()
    server.output_dir = tmp_path
    return server


def _call(server, args: dict) -> str:
    return asyncio.run(server.tools["memory_write"]["handler"](args))


def test_mcp_returns_similar_for_a_near_duplicate(tmp_path, fake):
    first = json.loads(_call(_server(tmp_path), {"title": "Postgres", "body": "relational"}))
    assert first["similar"] == [] and first["similar_available"] is True
    assert "superseded" not in first
    second = json.loads(_call(_server(tmp_path), {"title": "Postgres 16", "body": "database"}))
    assert [s["id"] for s in second["similar"]] == [first["memory_id"]]
    assert second["status"] == "stored" and second["kind"] == "note"


def test_mcp_similar_unavailable_without_backend(tmp_path, no_backend):
    payload = json.loads(_call(_server(tmp_path), {"title": "Postgres", "body": "relational"}))
    assert payload["similar"] == []
    assert payload["similar_available"] is False


def test_mcp_supersedes(tmp_path, fake):
    server = _server(tmp_path)
    old = json.loads(_call(server, {"title": "Postgres 15", "body": "relational"}))["memory_id"]
    payload = json.loads(
        _call(server, {"title": "Postgres 16", "body": "relational", "supersedes": old})
    )
    assert payload["superseded"] == old
    entry = get_memory(tmp_path, old)
    assert entry is not None and entry.superseded_by == payload["memory_id"]


@pytest.mark.parametrize("bad", ["abc", 1.5, True, 9999])
def test_mcp_supersedes_errors_are_plain_strings(tmp_path, no_backend, bad):
    server = _server(tmp_path)
    before = _counts(tmp_path)
    result = _call(server, {"title": "T", "body": "B", "supersedes": bad})
    assert isinstance(result, str) and not result.startswith("{")
    assert "supersedes" in result.lower() or "memory" in result.lower()
    assert _counts(tmp_path) == before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_args(tmp_path, **overrides) -> Namespace:
    values = {
        "output_dir": str(tmp_path),
        "memory_action": "add",
        "kind": "fact",
        "title": "Postgres 16",
        "body": "relational",
        "project": None,
        "author": None,
        "tag": None,
        "from_session": None,
        "supersedes": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_cli_add_prints_similar_and_supersedes(tmp_path, fake, capsys):
    old = add_memory(tmp_path, "fact", "Postgres 15", "relational")
    lore_cli.cmd_memory(_add_args(tmp_path))
    out = capsys.readouterr().out
    assert "Stored memory #" in out
    assert f"Similar existing memories: #{old} Postgres 15 (1.00)" in out

    lore_cli.cmd_memory(_add_args(tmp_path, title="Postgres 17", supersedes=old))
    entry = get_memory(tmp_path, old)
    assert entry is not None and entry.superseded_by is not None


def test_cli_add_supersedes_violation_exits_1(tmp_path, no_backend, capsys):
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_add_args(tmp_path, supersedes=4242))
    assert exc.value.code == 1
    assert "does not exist" in capsys.readouterr().err
    assert list_memory(tmp_path, status=None) == []


def test_cli_add_without_backend_prints_no_similar_line(tmp_path, no_backend, capsys):
    lore_cli.cmd_memory(_add_args(tmp_path))
    out = capsys.readouterr().out
    assert "Stored memory #" in out and "Similar" not in out


# ---------------------------------------------------------------------------
# Review fixes: transaction scope, id bounds, locked database, self-duplicate
# ---------------------------------------------------------------------------


class _KillSourceInsert:
    """Connection proxy whose memory_sources INSERT fails."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args):
        if "INTO memory_sources" in sql:
            raise sqlite3.OperationalError("injected failure")
        return self._conn.execute(sql, *args)

    def close(self) -> None:
        self._conn.close()


def test_validation_runs_inside_the_write_transaction(tmp_path, no_backend, monkeypatch):
    old = add_memory(tmp_path, "fact", "Old", "x")
    seen: list[bool] = []
    real_check = memory_mod._check_supersedable

    def spy(conn, target_id, scope_project):
        seen.append(conn.in_transaction)
        real_check(conn, target_id, scope_project)

    monkeypatch.setattr(memory_mod, "_check_supersedable", spy)
    add_memory(tmp_path, "fact", "New", "y", supersedes=old)
    assert seen == [True]


def test_source_link_failure_commits_nothing_and_retry_succeeds(tmp_path, no_backend, monkeypatch):
    old = add_memory(tmp_path, "fact", "Old", "x")
    before = _counts(tmp_path)
    real_connect = memory_mod._connect
    monkeypatch.setattr(memory_mod, "_connect", lambda d: _KillSourceInsert(real_connect(d)))
    with pytest.raises(sqlite3.OperationalError):
        add_memory(tmp_path, "fact", "New", "y", supersedes=old, source_session="sess-1")
    monkeypatch.setattr(memory_mod, "_connect", real_connect)

    assert _counts(tmp_path) == before
    entry = get_memory(tmp_path, old)
    assert entry is not None and entry.superseded_by is None

    new = add_memory(tmp_path, "fact", "New", "y", supersedes=old, source_session="sess-1")
    entry = get_memory(tmp_path, old)
    assert entry is not None and entry.superseded_by == new
    new_entry = get_memory(tmp_path, new)
    assert new_entry is not None
    assert new_entry.sources == [{"session_id": "sess-1", "note": "recorded from this session"}]


def test_unserializable_metadata_raises_before_any_write(tmp_path, no_backend):
    before = _counts(tmp_path)
    with pytest.raises(ValueError, match="JSON-serializable"):
        add_memory(tmp_path, "fact", "T", "b", metadata={"x": object()})
    assert _counts(tmp_path) == before


@pytest.mark.parametrize("bad", [2**63, 10**30, 0, -1, True, "1"])
def test_out_of_range_ids_raise_value_error(tmp_path, no_backend, bad):
    mid = add_memory(tmp_path, "fact", "T", "b")
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "N", "y", supersedes=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        record_feedback(tmp_path, bad, "agent", 3)  # type: ignore[arg-type]
    assert get_memory(tmp_path, mid) is not None


def test_out_of_range_ids_are_not_found_for_get_delete_status(tmp_path, no_backend):
    assert get_memory(tmp_path, 2**63) is None
    assert delete_memory(tmp_path, 10**30) is False
    assert set_memory_status(tmp_path, 2**63, "confirmed") is False


def test_mcp_out_of_range_ids_are_plain_strings(tmp_path, no_backend):
    server = _server(tmp_path)
    result = _call(server, {"title": "T", "body": "B", "supersedes": 2**63})
    assert isinstance(result, str) and result.startswith("Could not write memory")
    feedback = asyncio.run(
        server.tools["memory_feedback"]["handler"]({"memory_id": 2**63, "score": 3})
    )
    assert feedback.startswith("Could not record feedback")


def test_cli_rate_out_of_range_id_exits_one(tmp_path, no_backend, capsys):
    args = Namespace(
        output_dir=str(tmp_path), memory_action="rate", memory_id=2**63, score=3, rater="a"
    )
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(args)
    assert exc.value.code == 1
    assert "Could not rate memory" in capsys.readouterr().err


@pytest.fixture
def locked_db(tmp_path, no_backend, monkeypatch):
    """The v2 DB with another connection holding the write lock; busy timeout 50 ms."""
    import lore.storage.feedback as feedback_mod
    from lore.storage.writer import v2_db_path

    add_memory(tmp_path, "fact", "Existing", "x")  # creates and migrates the DB
    for module in (memory_mod, feedback_mod):
        real = module._connect

        def short(d, real=real):
            conn = real(d)
            conn.execute("PRAGMA busy_timeout = 50")
            return conn

        monkeypatch.setattr(module, "_connect", short)
    blocker = sqlite3.connect(str(v2_db_path(tmp_path)), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        yield tmp_path
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_mcp_write_on_locked_database_is_a_plain_string(locked_db):
    result = _call(_server(locked_db), {"title": "T", "body": "B"})
    assert result == "Could not write memory: database is busy, try again"


def test_mcp_feedback_on_locked_database_is_a_plain_string(locked_db):
    handler = _server(locked_db).tools["memory_feedback"]["handler"]
    result = asyncio.run(handler({"memory_id": 1, "score": 4}))
    assert result == "Could not write memory: database is busy, try again"


def test_cli_add_and_rate_on_locked_database_exit_one(locked_db, capsys):
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_add_args(locked_db))
    assert exc.value.code == 1
    rate = Namespace(
        output_dir=str(locked_db), memory_action="rate", memory_id=1, score=3, rater="a"
    )
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(rate)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.count("Could not write memory: database is busy, try again") == 2
    assert "Traceback" not in err


def test_similar_excludes_the_superseded_target(tmp_path, fake):
    old = add_memory(tmp_path, "fact", "Postgres 15", "relational")
    other = add_memory(tmp_path, "fact", "Postgres 14", "relational")
    hits = find_similar_memories(tmp_path, "postgres", scope_project=None, exclude_id=old)
    assert hits is not None and {h["id"] for h in hits} == {other}


def test_mcp_supersedes_does_not_list_the_target_as_similar(tmp_path, fake):
    server = _server(tmp_path)
    old = json.loads(_call(server, {"title": "Postgres 15", "body": "relational"}))["memory_id"]
    payload = json.loads(
        _call(server, {"title": "Postgres 16", "body": "relational", "supersedes": old})
    )
    assert old not in [s["id"] for s in payload["similar"]]


def test_cli_supersedes_does_not_list_the_target_as_similar(tmp_path, fake, capsys):
    old = add_memory(tmp_path, "fact", "Postgres 15", "relational")
    lore_cli.cmd_memory(_add_args(tmp_path, title="Postgres 17", supersedes=old))
    assert "Similar" not in capsys.readouterr().out
