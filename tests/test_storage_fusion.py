"""Tests for Reciprocal Rank Fusion (#87, PR 3).

Pure functions — no backend needed, always run.
"""

from __future__ import annotations

from annuli.storage.fusion import DEFAULT_K, rrf_merge


def _r(sid: str, score: float = 0.0) -> dict:
    return {"session": {"id": sid}, "score": score}


def test_empty_inputs_return_empty():
    assert rrf_merge() == []
    assert rrf_merge([], []) == []


def test_single_list_preserves_order():
    lst = [_r("a"), _r("b"), _r("c")]
    fused = rrf_merge(lst)
    assert [f["session"]["id"] for f in fused] == ["a", "b", "c"]


def test_document_in_both_lists_ranks_above_singletons():
    # 'b' appears in both lists → gets two RRF contributions, so it should
    # outrank 'a' and 'x' which each appear once, even at rank 0.
    fts = [_r("a"), _r("b")]
    vec = [_r("b"), _r("x")]
    fused = rrf_merge(fts, vec)
    assert fused[0]["session"]["id"] == "b"


def test_rrf_score_math():
    # 'a' at rank 0 in list1 only: score = 1/(k+0).
    fused = rrf_merge([_r("a")], [_r("b")])
    a = next(f for f in fused if f["session"]["id"] == "a")
    assert a["score"] == 1.0 / (DEFAULT_K + 0)


def test_score_field_replaced_with_rrf():
    # Input scores (bm25/distance) are discarded; output carries the RRF score.
    fused = rrf_merge([_r("a", score=99.0)])
    assert fused[0]["score"] != 99.0
    assert fused[0]["score"] == 1.0 / DEFAULT_K


def test_limit_truncates():
    lst = [_r(str(i)) for i in range(10)]
    assert len(rrf_merge(lst, limit=3)) == 3


def test_ties_break_deterministically_by_key():
    # Two docs at the same rank in separate lists tie on RRF score; order must
    # be stable (by id) so results don't flap between calls.
    fused = rrf_merge([_r("z")], [_r("a")])
    assert [f["session"]["id"] for f in fused] == ["a", "z"]


def test_missing_id_is_skipped():
    fused = rrf_merge([{"session": {}, "score": 1.0}, _r("a")])
    assert [f["session"].get("id") for f in fused] == ["a"]


def test_first_seen_object_is_kept():
    # The returned object keeps the full session dict from its first appearance.
    rich = {"session": {"id": "a", "title": "Rich"}, "score": 0.0}
    plain = {"session": {"id": "a"}, "score": 0.0}
    fused = rrf_merge([rich], [plain])
    assert fused[0]["session"]["title"] == "Rich"
