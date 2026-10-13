"""Tests for memory feedback: storage, derived weight, ranking hook, N+1 (#197)."""

from __future__ import annotations

from argparse import Namespace

import pytest

import lore.storage.memory as memory_mod
import lore_cli
from lore.storage import (
    add_memory,
    delete_memory,
    feedback_influence,
    feedback_weight,
    get_memory,
    list_memory,
    ranking_factor,
    recall_memory,
    record_feedback,
)
from lore.storage.schema import MIGRATIONS, current_version, open_connection
from lore.storage.writer import v2_db_path


def _mem(tmp_path, title: str, body: str = "alpha beta") -> int:
    return add_memory(tmp_path, "note", title, body)


def _ids(entries) -> list[int]:
    return [e.id for e in entries]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_15_from_v14_keeps_rows(tmp_path):
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 14]
    try:
        memory_id = _mem(tmp_path, "keep me")
    finally:
        MIGRATIONS[:] = full
    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) == 14
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_feedback'"
        ).fetchone()
    finally:
        conn.close()

    assert feedback_weight(tmp_path, memory_id) == (0.5, 0)  # opens and migrates

    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) >= 15
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_feedback'").fetchone()
    finally:
        conn.close()
    entry = get_memory(tmp_path, memory_id)
    assert entry is not None and entry.title == "keep me"


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------


def test_no_rows_is_neutral(tmp_path):
    memory_id = _mem(tmp_path, "unrated")
    assert feedback_weight(tmp_path, memory_id) == (0.5, 0)


def test_rerating_replaces_instead_of_stacking(tmp_path):
    once = _mem(tmp_path, "once")
    many = _mem(tmp_path, "many")
    record_feedback(tmp_path, once, "agent", 5)
    for _ in range(50):
        record_feedback(tmp_path, many, "agent", 5)
    assert feedback_weight(tmp_path, many) == feedback_weight(tmp_path, once)
    assert feedback_weight(tmp_path, many)[1] == 1


def test_rerating_changes_the_score(tmp_path):
    memory_id = _mem(tmp_path, "m")
    record_feedback(tmp_path, memory_id, "agent", 5)
    record_feedback(tmp_path, memory_id, "agent", 1)
    # one score of 1: (0 + 1.5) / (1 + 3)
    assert feedback_weight(tmp_path, memory_id) == (pytest.approx(0.375), 1)


def test_two_raters_average_with_prior(tmp_path):
    memory_id = _mem(tmp_path, "m")
    record_feedback(tmp_path, memory_id, "a", 5)
    record_feedback(tmp_path, memory_id, "b", 3)
    # scores (5-1)/4 = 1.0 and (3-1)/4 = 0.5; (1.0 + 0.5 + 1.5) / (2 + 3) = 0.6
    weight, count = feedback_weight(tmp_path, memory_id)
    assert weight == pytest.approx(0.6)
    assert count == 2


@pytest.mark.parametrize(
    "rater, score",
    [
        ("agent", 0),
        ("agent", 6),
        ("agent", True),
        ("agent", "5"),
        ("", 3),
        ("   ", 3),
        ("x" * 65, 3),
        ("bad\x00name", 3),
        ("bad\nname", 3),
    ],
)
def test_validation_errors(tmp_path, rater, score):
    memory_id = _mem(tmp_path, "m")
    with pytest.raises(ValueError):
        record_feedback(tmp_path, memory_id, rater, score)  # type: ignore[arg-type]
    assert feedback_weight(tmp_path, memory_id) == (0.5, 0)


def test_unknown_memory_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        record_feedback(tmp_path, 999, "agent", 3)


def test_rater_is_stripped_and_64_chars_allowed(tmp_path):
    memory_id = _mem(tmp_path, "m")
    record_feedback(tmp_path, memory_id, "  agent  ", 4)
    record_feedback(tmp_path, memory_id, "agent", 2)  # same rater after stripping
    record_feedback(tmp_path, memory_id, "y" * 64, 4)
    assert feedback_weight(tmp_path, memory_id)[1] == 2


def test_deleting_a_memory_removes_its_feedback(tmp_path):
    memory_id = _mem(tmp_path, "m")
    record_feedback(tmp_path, memory_id, "agent", 5)
    assert delete_memory(tmp_path, memory_id)
    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_feedback").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ranking factor and influence
# ---------------------------------------------------------------------------


def test_ranking_factor_is_bounded():
    for step in range(101):
        w = step / 100
        assert 0.75 <= ranking_factor(w, 1.0) <= 1.25
        assert ranking_factor(w, 0.0) == 1.0
    assert ranking_factor(0.5, 1.0) == 1.0
    assert ranking_factor(1.0, 1.0) == pytest.approx(1.25)
    assert ranking_factor(0.0, 1.0) == pytest.approx(0.75)


def test_hundred_sybil_raters_cannot_exceed_the_bound(tmp_path):
    memory_id = _mem(tmp_path, "m")
    for i in range(100):
        record_feedback(tmp_path, memory_id, f"sybil-{i}", 5)
    weight, count = feedback_weight(tmp_path, memory_id)
    assert count == 100
    assert ranking_factor(weight, 1.0) <= 1.25


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 0.0),
        ("", 0.0),
        ("garbage", 0.0),
        ("nan", 0.0),
        ("0.5", 0.5),
        ("7", 1.0),
        ("-2", 0.0),
    ],
)
def test_feedback_influence_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LORE_FEEDBACK_INFLUENCE", raising=False)
    else:
        monkeypatch.setenv("LORE_FEEDBACK_INFLUENCE", raw)
    assert feedback_influence() == expected


# ---------------------------------------------------------------------------
# Recall: payload and ranking hook
# ---------------------------------------------------------------------------


def _three(tmp_path):
    first = _mem(tmp_path, "first", "alpha alpha alpha beta")
    second = _mem(tmp_path, "second", "alpha beta gamma delta epsilon")
    third = _mem(tmp_path, "third", "alpha beta gamma delta epsilon zeta eta theta")
    return first, second, third


def test_entries_carry_weight_and_count_on_every_path(tmp_path):
    first, second, _third = _three(tmp_path)
    record_feedback(tmp_path, second, "a", 5)
    record_feedback(tmp_path, second, "b", 5)

    def check(entries):
        by_id = {e.id: e for e in entries}
        assert by_id[second].feedback_count == 2
        assert by_id[second].feedback_weight == pytest.approx(0.7)
        assert by_id[first].feedback_count == 0
        assert by_id[first].feedback_weight == 0.5
        assert by_id[second].to_dict()["feedback_count"] == 2

    check(recall_memory(tmp_path, "alpha", semantic=False))
    check(list_memory(tmp_path))
    check(recall_memory(tmp_path, ""))
    entry = get_memory(tmp_path, second)
    assert entry is not None
    check([entry, *list_memory(tmp_path)])


def test_influence_unset_keeps_order(tmp_path, monkeypatch):
    first, second, third = _three(tmp_path)
    monkeypatch.delenv("LORE_FEEDBACK_INFLUENCE", raising=False)
    baseline = _ids(recall_memory(tmp_path, "alpha", semantic=False))
    assert sorted(baseline) == sorted([first, second, third])

    for rater in ("a", "b", "c", "d"):
        record_feedback(tmp_path, baseline[-1], rater, 5)
        record_feedback(tmp_path, baseline[0], rater, 1)

    assert _ids(recall_memory(tmp_path, "alpha", semantic=False)) == baseline
    monkeypatch.setenv("LORE_FEEDBACK_INFLUENCE", "0")
    assert _ids(recall_memory(tmp_path, "alpha", semantic=False)) == baseline


def test_unset_influence_never_reorders(tmp_path, monkeypatch):
    _three(tmp_path)
    monkeypatch.delenv("LORE_FEEDBACK_INFLUENCE", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("ranking_factor must not run with influence 0")

    monkeypatch.setattr(memory_mod, "ranking_factor", boom)
    assert recall_memory(tmp_path, "alpha", semantic=False)


def test_influence_lifts_rated_up_and_sinks_rated_down(tmp_path, monkeypatch):
    _three(tmp_path)
    monkeypatch.delenv("LORE_FEEDBACK_INFLUENCE", raising=False)
    baseline = _ids(recall_memory(tmp_path, "alpha", semantic=False))
    top, middle, bottom = baseline

    for rater in ("a", "b", "c", "d", "e"):
        record_feedback(tmp_path, bottom, rater, 5)
    monkeypatch.setenv("LORE_FEEDBACK_INFLUENCE", "1")
    lifted = _ids(recall_memory(tmp_path, "alpha", semantic=False))
    assert lifted.index(bottom) < baseline.index(bottom)

    for rater in ("a", "b", "c", "d", "e"):
        record_feedback(tmp_path, top, rater, 1)
    sunk = _ids(recall_memory(tmp_path, "alpha", semantic=False))
    assert sunk.index(top) > baseline.index(top)
    assert sunk[0] == bottom
    assert sorted(sunk) == sorted(baseline)


def test_ranking_is_stable_on_ties(tmp_path, monkeypatch):
    _three(tmp_path)
    monkeypatch.delenv("LORE_FEEDBACK_INFLUENCE", raising=False)
    baseline = _ids(recall_memory(tmp_path, "alpha", semantic=False))
    monkeypatch.setenv("LORE_FEEDBACK_INFLUENCE", "1")
    assert _ids(recall_memory(tmp_path, "alpha", semantic=False)) == baseline


# ---------------------------------------------------------------------------
# N+1 regression: tags/sources/feedback are loaded in constant statements
# ---------------------------------------------------------------------------


def _statements_for(monkeypatch, run) -> dict[str, int]:
    seen: list[str] = []
    original = memory_mod._connect

    def traced(output_dir):
        conn = original(output_dir)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(memory_mod, "_connect", traced)
    run()
    return {
        table: sum(1 for s in seen if f"FROM {table}" in s)
        for table in ("memory_tags", "memory_sources", "memory_feedback")
    }


def _populate(tmp_path, count: int) -> None:
    for i in range(count):
        memory_id = add_memory(
            tmp_path,
            "note",
            f"memory {i}",
            f"alpha number {i}",
            tags=[f"t{i}"],
            source_session=f"session-{i}",
        )
        record_feedback(tmp_path, memory_id, "agent", 4)


def test_recall_statement_count_is_constant(tmp_path, monkeypatch):
    _populate(tmp_path, 20)
    big = _statements_for(
        monkeypatch, lambda: recall_memory(tmp_path, "alpha", semantic=False, limit=20)
    )
    assert big == {"memory_tags": 1, "memory_sources": 1, "memory_feedback": 1}

    listed = _statements_for(monkeypatch, lambda: list_memory(tmp_path, limit=20))
    assert listed == {"memory_tags": 1, "memory_sources": 1, "memory_feedback": 1}


def test_fts_overfetch_only_loads_kept_rows(tmp_path, monkeypatch):
    _populate(tmp_path, 12)
    # A kind filter that drops every row must not trigger per-row loading.
    stats = _statements_for(
        monkeypatch,
        lambda: recall_memory(tmp_path, "alpha", kind="fact", semantic=False, limit=3),
    )
    assert all(count <= 1 for count in stats.values())


# ---------------------------------------------------------------------------
# CLI: lore memory rate
# ---------------------------------------------------------------------------


def _rate_args(tmp_path, memory_id, score, rater="alice"):
    return Namespace(
        output_dir=str(tmp_path),
        memory_action="rate",
        memory_id=memory_id,
        score=score,
        rater=rater,
    )


def test_cli_rate_records_and_prints_the_weight(tmp_path, capsys):
    mid = add_memory(tmp_path, "fact", "T", "body")

    lore_cli.cmd_memory(_rate_args(tmp_path, mid, 5))
    lore_cli.cmd_memory(_rate_args(tmp_path, mid, 2, rater="bob"))

    out = capsys.readouterr().out
    assert "Rated memory #1 5/5 (weight 0.62, n=1)." in out
    assert "weight 0.55, n=2" in out


@pytest.mark.parametrize("score", [0, 6])
def test_cli_rate_rejects_bad_score_with_exit_one(tmp_path, capsys, score):
    mid = add_memory(tmp_path, "fact", "T", "body")

    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_rate_args(tmp_path, mid, score))

    assert exc.value.code == 1
    assert "score must be an integer from 1 to 5" in capsys.readouterr().err


def test_cli_rate_unknown_memory_exits_one(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_rate_args(tmp_path, 999, 3))

    assert exc.value.code == 1
    assert "does not exist" in capsys.readouterr().err
