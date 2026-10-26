#!/usr/bin/env python3
"""Calibrate the auto-mode recall floor against a labelled pair set (#199).

Runs the real embedding model over ``tools/calibration/recall-floor-
labelled-pairs.json`` (stored memory vs. user query, labelled related/unrelated)
and reports the cosine distributions plus a recommended
``LORE_MEMORY_RECALL_MIN_SIMILARITY``.

The model comes from the optional ``lore[semantic]`` extra, so run this from an
environment that has ``fastembed`` installed — never from a unit test run (tests
must not download models):

    .venv/bin/python tools/calibration/calibrate_recall_floor.py
    .venv/bin/python tools/calibration/calibrate_recall_floor.py --json   # machine-readable

Only text pairs are read from disk; nothing leaves the machine (local model).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PAIRS = REPO / "tools" / "calibration" / "recall-floor-labelled-pairs.json"


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine similarity, the same definition as lore.storage.embeddings."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile, ``pct`` in 0..100."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def recommend_floor(related: List[float], unrelated: List[float]) -> Tuple[float, float]:
    """Pick the floor that separates the groups best, plus its balanced accuracy.

    Sweeps every observed score as a candidate cut and keeps the one with the
    highest balanced accuracy (equal weight on keeping related hits and dropping
    unrelated padding); ties go to the higher floor, because a missing hit is
    cheaper than padded recall.
    """
    candidates = sorted({*related, *unrelated})
    best_floor, best_accuracy = 0.5, -1.0
    for floor in candidates:
        kept_related = sum(1 for s in related if s >= floor) / max(1, len(related))
        dropped_unrelated = sum(1 for s in unrelated if s < floor) / max(1, len(unrelated))
        accuracy = (kept_related + dropped_unrelated) / 2
        if accuracy > best_accuracy or (accuracy == best_accuracy and floor > best_floor):
            best_floor, best_accuracy = floor, accuracy
    return best_floor, best_accuracy


def load_scores(pairs_path: Path, embed) -> Dict[str, List[float]]:
    """Cosine similarity of every labelled pair, grouped by label."""
    data: Dict[str, Any] = json.loads(pairs_path.read_text(encoding="utf-8"))
    scores: Dict[str, List[float]] = {"related": [], "unrelated": []}
    for pair in data["pairs"]:
        label = str(pair["label"])
        if label not in scores:
            raise ValueError(f"unknown label {label!r} in pair {pair!r}")
        scores[label].append(cosine(embed(str(pair["memory"])), embed(str(pair["query"]))))
    return scores


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

    scores = load_scores(Path(args.pairs), embed_text)
    related, unrelated = scores["related"], scores["unrelated"]
    if not related or not unrelated:
        print("NOT VERIFIED: the pair set needs both labels", file=sys.stderr)
        return 2
    floor, accuracy = recommend_floor(related, unrelated)

    if args.json:
        print(
            json.dumps(
                {
                    "model": DEFAULT_MODEL,
                    "pairs": len(related) + len(unrelated),
                    "related_min": round(min(related), 4),
                    "related_p05": round(percentile(related, 5), 4),
                    "related_median": round(statistics.median(related), 4),
                    "unrelated_median": round(statistics.median(unrelated), 4),
                    "unrelated_p95": round(percentile(unrelated, 95), 4),
                    "unrelated_max": round(max(unrelated), 4),
                    "recommended_floor": round(floor, 3),
                    "balanced_accuracy": round(accuracy, 4),
                    "overlap": min(related) <= max(unrelated),
                },
                indent=2,
            )
        )
        return 0

    print(f"model: {DEFAULT_MODEL}   pairs: {len(related) + len(unrelated)}")
    print(
        f"related   n={len(related):3d}  min={min(related):.3f}  "
        f"p05={percentile(related, 5):.3f}  median={statistics.median(related):.3f}  "
        f"max={max(related):.3f}"
    )
    print(
        f"unrelated n={len(unrelated):3d}  min={min(unrelated):.3f}  "
        f"median={statistics.median(unrelated):.3f}  p95={percentile(unrelated, 95):.3f}  "
        f"max={max(unrelated):.3f}"
    )
    print(f"recommended floor: {floor:.3f}  (balanced accuracy {accuracy:.3f})")
    if min(related) <= max(unrelated):
        print("warning: the groups overlap — a single floor cannot separate them perfectly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
