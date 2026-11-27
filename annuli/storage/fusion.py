"""Reciprocal Rank Fusion for hybrid search (#87, PR 3).

Combines several independently-ranked result lists (here: FTS keyword hits and
sqlite-vec semantic hits) into one ranking without needing their scores to be
on a comparable scale. Each document scores ``Σ 1/(k + rank)`` across the lists
it appears in, where ``rank`` is its 0-based position in that list.

RRF is deliberately score-agnostic: bm25 (smaller = better) and vector distance
(smaller = better) live on different scales, so fusing by *rank* avoids any
normalization or weight tuning. ``k`` damps the contribution of low-ranked
items; 60 is the value from the original RRF paper and the common default.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Sequence

# Each result is the standard ``{"session": <dict>, "score": <float>}`` shape.
Result = Dict[str, Any]

DEFAULT_K = 60


def _default_key(result: Result) -> str:
    """Identity of a result for fusion — the session id."""
    return str(result.get("session", {}).get("id", ""))


def rrf_merge(
    *ranked_lists: Sequence[Result],
    k: int = DEFAULT_K,
    limit: int = 50,
    key: Callable[[Result], str] = _default_key,
) -> List[Result]:
    """Fuse ranked result lists by Reciprocal Rank Fusion.

    Args:
        ranked_lists: one or more already-ordered result lists (best first).
        k: RRF damping constant (paper default 60).
        limit: max fused results to return.
        key: identity function; results sharing a key are the same document.

    Returns:
        A single list ordered by descending RRF score. Each returned result is
        the first-seen object for its key (so it keeps a ``session`` dict), with
        its ``score`` replaced by the fused RRF score. Ties break by key for a
        stable, deterministic order.
    """
    rrf_score: Dict[str, float] = {}
    first_seen: Dict[str, Result] = {}

    for results in ranked_lists:
        for rank, result in enumerate(results):
            ident = key(result)
            if not ident:
                continue
            rrf_score[ident] = rrf_score.get(ident, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(ident, result)

    ordered = sorted(rrf_score, key=lambda i: (-rrf_score[i], i))
    fused: List[Result] = []
    for ident in ordered[:limit]:
        result = dict(first_seen[ident])
        result["score"] = rrf_score[ident]
        fused.append(result)
    return fused
