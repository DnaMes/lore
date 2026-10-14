"""B6.1: ratings only on confirmed memories."""

from __future__ import annotations

import pytest

from lore.storage import (
    add_memory,
    feedback_weight,
    recall_memory,
    record_feedback,
)
from lore.storage.memory import set_memory_status


def test_record_feedback_rejects_candidate(tmp_path):
    mid = add_memory(tmp_path, "note", "draft", "body", status="candidate")
    with pytest.raises(ValueError, match="not confirmed"):
        record_feedback(tmp_path, mid, "agent", 5)


def test_record_feedback_rejects_rejected(tmp_path):
    mid = add_memory(tmp_path, "note", "bad", "body", status="candidate")
    set_memory_status(tmp_path, mid, "rejected")
    with pytest.raises(ValueError, match="not confirmed"):
        record_feedback(tmp_path, mid, "agent", 3)


def test_record_feedback_unknown_id(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        record_feedback(tmp_path, 4242, "agent", 3)


def test_record_feedback_confirmed_works(tmp_path):
    mid = add_memory(tmp_path, "note", "good", "body", status="confirmed")
    record_feedback(tmp_path, mid, "agent", 5)
    assert feedback_weight(tmp_path, mid) == (pytest.approx(0.625), 1)


def test_rejected_after_rating_keeps_rows_but_not_in_recall(tmp_path, monkeypatch):
    from lore.storage.schema import open_v2_connection
    from lore.storage.writer import v2_db_path

    monkeypatch.setenv("LORE_FEEDBACK_INFLUENCE", "1")
    low = add_memory(tmp_path, "note", "low rated", "alpha beta gamma")
    high = add_memory(tmp_path, "note", "high rated", "alpha beta gamma")
    record_feedback(tmp_path, low, "r", 1)
    record_feedback(tmp_path, high, "r", 5)
    conn = open_v2_connection(v2_db_path(tmp_path))
    conn.execute("UPDATE memory SET status = 'rejected' WHERE id = ?", (low,))
    conn.commit()
    conn.close()
    assert feedback_weight(tmp_path, low)[1] == 1
    ids = [e.id for e in recall_memory(tmp_path, "alpha", limit=10)]
    assert high in ids
    assert low not in ids
