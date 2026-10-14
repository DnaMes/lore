#!/usr/bin/env python3
"""Calibrate the hybrid-search distance cutoff against labelled session pairs (#92).

Runs the real embedding model over
``docs/research/2026-10-14-semantic-cutoff-labelled-pairs.json`` (session body vs.
search query, labelled related/unrelated) and reports the **L2** distance
distribution — the metric sqlite-vec uses on ``session_embeddings`` (vec0's
default; the vectors are unit-normalised, so ``distance = sqrt(2 - 2*cosine)``).

The recommended cutoff is chosen by sweeping the observed distances and keeping
the one with the highest balanced accuracy (equal weight on keeping related
sessions and dropping unrelated padding), ties going to the stricter (lower)
cutoff. Reported alongside: which percentile of the *related* distribution the
cutoff sits at, so the value is never a bare constant.

    python tools/calibrate_semantic_cutoff.py
    python tools/calibrate_semantic_cutoff.py --json

Needs the optional ``lore[semantic]`` extra (fastembed + model); never run it
from a unit test — tests must not download models.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PAIRS = REPO / "docs" / "research" / "2026-10-14-semantic-cutoff-labelled-pairs.json"


def l2_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Euclidean distance, the metric vec0 uses by default."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile, ``pct`` in 0..100."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def recommend_cutoff(related: List[float], unrelated: List[float]) -> Tuple[float, float]:
    """Cutoff plus its balanced accuracy; ties prefer the stricter (lower) cutoff."""
    candidates = sorted({*related, *unrelated})
    best_cutoff, best_accuracy = float("inf"), -1.0
    for cutoff in candidates:
        kept = sum(1 for d in related if d <= cutoff) / max(1, len(related))
        dropped = sum(1 for d in unrelated if d > cutoff) / max(1, len(unrelated))
        accuracy = (kept + dropped) / 2
        if accuracy > best_accuracy or (accuracy == best_accuracy and cutoff < best_cutoff):
            best_cutoff, best_accuracy = cutoff, accuracy
    return best_cutoff, best_accuracy


def load_distances(pairs_path: Path, embed) -> Tuple[Dict[str, List[float]], List[float]]:
    """Distances per label, plus the embedding norms (they must be ~1 for L2==chord)."""
    data = json.loads(pairs_path.read_text(encoding="utf-8"))
    scores: Dict[str, List[float]] = {"related": [], "unrelated": []}
    norms: List[float] = []
    for pair in data["pairs"]:
        label = str(pair["label"])
        if label not in scores:
            raise ValueError(f"unknown label {label!r} in pair {pair!r}")
        session_vec = embed(str(pair["session"]))
        query_vec = embed(str(pair["query"]))
        norms.append(norm(session_vec))
        scores[label].append(l2_distance(session_vec, query_vec))
    return scores, norms


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS, help="labelled pair set")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        from lore.storage.embeddings import DEFAULT_MODEL, embed_text
    except ImportError as exc:
        print(f"NOT VERIFIED: lore is not importable ({exc})", file=sys.stderr)
        return 2
    if embed_text("warmup") is None:
        print(
            "NOT VERIFIED: the embedding backend is unavailable — install the "
            "'semantic' extra (fastembed) and let it fetch the model first.",
            file=sys.stderr,
        )
        return 2

    scores, norms = load_distances(Path(args.pairs), embed_text)
    related, unrelated = scores["related"], scores["unrelated"]
    if not related or not unrelated:
        print("NOT VERIFIED: the pair set needs both labels", file=sys.stderr)
        return 2
    cutoff, accuracy = recommend_cutoff(related, unrelated)
    # Percentile of the related distribution the cutoff sits at: how many related
    # sessions would it keep? (larger = looser)
    kept_related = sum(1 for d in related if d <= cutoff) / len(related)
    related_p95 = percentile(related, 95)

    if args.json:
        print(
            json.dumps(
                {
                    "model": DEFAULT_MODEL,
                    "pairs": len(related) + len(unrelated),
                    "vector_norm_mean": round(statistics.mean(norms), 6),
                    "related_min": round(min(related), 4),
                    "related_median": round(statistics.median(related), 4),
                    "related_p95": round(related_p95, 4),
                    "related_max": round(max(related), 4),
                    "unrelated_min": round(min(unrelated), 4),
                    "unrelated_p05": round(percentile(unrelated, 5), 4),
                    "unrelated_median": round(statistics.median(unrelated), 4),
                    "unrelated_max": round(max(unrelated), 4),
                    "recommended_cutoff": round(cutoff, 4),
                    "balanced_accuracy": round(accuracy, 4),
                    "related_kept_at_cutoff": round(kept_related, 4),
                },
                indent=2,
            )
        )
        return 0

    print(
        f"model: {DEFAULT_MODEL}   metric: L2 (vec0 default)   pairs: {len(related) + len(unrelated)}"
    )
    print(
        f"vector norm: mean {statistics.mean(norms):.4f} (1.0 = unit-normalised, L2 == chord length)"
    )
    print(
        f"related   n={len(related):3d}  min={min(related):.3f}  "
        f"median={statistics.median(related):.3f}  p95={related_p95:.3f}  max={max(related):.3f}"
    )
    print(
        f"unrelated n={len(unrelated):3d}  min={min(unrelated):.3f}  "
        f"p05={percentile(unrelated, 5):.3f}  median={statistics.median(unrelated):.3f}  "
        f"max={max(unrelated):.3f}"
    )
    print(
        f"recommended cutoff: {cutoff:.3f}  (keeps {kept_related * 100:.0f}% of related, "
        f"balanced accuracy {accuracy:.3f})"
    )
    if max(related) < min(unrelated):
        gap_lo, gap_hi = max(related), min(unrelated)
        midpoint = (related_p95 + percentile(unrelated, 5)) / 2
        print(
            f"note: the groups are separated (related max {gap_lo:.3f} < unrelated min "
            f"{gap_hi:.3f}); midpoint of related p95 / unrelated p05 = {midpoint:.3f}"
        )
    else:
        print("warning: the groups overlap — no cutoff separates them perfectly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
