#!/usr/bin/env python3
"""Calibrate the message-level distance cutoff against the #190 evaluation set.

Runs the real embedding model over ``tools/calibration/message-retrieval-eval.json``
and reports the **L2** distance distribution — the metric sqlite-vec uses on
``message_embeddings`` (vec0's default; the vectors are unit-normalised, so
``distance = sqrt(2 - 2*cosine)``).

A pair is *related* when a query labels that message as the passage answering it,
*unrelated* when the message belongs to a session the query's labels do not touch
(a near-miss inside the labelled session is neither: it is topically adjacent by
construction and would only blur the boundary). The recommended cutoff is the
midpoint of related-p95 and unrelated-p05, the same convention the session-level
cutoff uses, and it is printed next to the balanced-accuracy sweep result so the
value is never a bare constant.

    python tools/calibration/calibrate_message_cutoff.py
    python tools/calibration/calibrate_message_cutoff.py --json

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

REPO = Path(__file__).resolve().parents[2]
DEFAULT_EVAL = REPO / "tools" / "calibration" / "message-retrieval-eval.json"


def l2_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Euclidean distance, the metric vec0 uses by default."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile, ``pct`` in 0..100."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))]


def recommend_cutoff(related: List[float], unrelated: List[float]) -> Tuple[float, float]:
    """Best balanced-accuracy cutoff; ties prefer the stricter (lower) cutoff."""
    best_cutoff, best_accuracy = float("inf"), -1.0
    for cutoff in sorted({*related, *unrelated}):
        kept = sum(1 for d in related if d <= cutoff) / max(1, len(related))
        dropped = sum(1 for d in unrelated if d > cutoff) / max(1, len(unrelated))
        accuracy = (kept + dropped) / 2
        if accuracy > best_accuracy or (accuracy == best_accuracy and cutoff < best_cutoff):
            best_cutoff, best_accuracy = cutoff, accuracy
    return best_cutoff, best_accuracy


def load_distances(eval_path: Path, embed) -> Dict[str, List[float]]:
    """Split query/message distances into the ``related`` and ``unrelated`` labels."""
    data = json.loads(eval_path.read_text(encoding="utf-8"))
    embeddings = {
        (session["id"], seq): embed(str(message["content"]))
        for session in data["sessions"]
        for seq, message in enumerate(session["messages"])
    }
    scores: Dict[str, List[float]] = {"related": [], "unrelated": []}
    for case in data["queries"]:
        if case.get("negative"):
            continue
        query_vector = embed(str(case["query"]))
        labelled = {(item["session"], int(item["seq"])) for item in case["relevant"]}
        labelled_sessions = {item["session"] for item in case["relevant"]}
        for key, vector in embeddings.items():
            distance = l2_distance(vector, query_vector)
            if key in labelled:
                scores["related"].append(distance)
            elif key[0] not in labelled_sessions:
                scores["unrelated"].append(distance)
    return scores


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL, help="evaluation set")
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

    scores = load_distances(Path(args.eval), embed_text)
    related, unrelated = scores["related"], scores["unrelated"]
    if not related or not unrelated:
        print("NOT VERIFIED: the evaluation set needs both labels", file=sys.stderr)
        return 2
    sweep_cutoff, accuracy = recommend_cutoff(related, unrelated)
    related_p95 = percentile(related, 95)
    unrelated_p05 = percentile(unrelated, 5)
    midpoint = (related_p95 + unrelated_p05) / 2

    if args.json:
        print(
            json.dumps(
                {
                    "model": DEFAULT_MODEL,
                    "related": len(related),
                    "related_min": round(min(related), 4),
                    "related_p95": round(related_p95, 4),
                    "related_max": round(max(related), 4),
                    "unrelated": len(unrelated),
                    "unrelated_min": round(min(unrelated), 4),
                    "unrelated_p05": round(unrelated_p05, 4),
                    "unrelated_median": round(statistics.median(unrelated), 4),
                    "recommended_cutoff": round(midpoint, 4),
                    "sweep_cutoff": round(sweep_cutoff, 4),
                    "balanced_accuracy": round(accuracy, 4),
                },
                indent=2,
            )
        )
        return 0

    print(
        f"model: {DEFAULT_MODEL}   metric: L2 (vec0 default)   "
        f"related {len(related)}   unrelated {len(unrelated)}"
    )
    print(
        f"related   min={min(related):.3f}  median={statistics.median(related):.3f}  "
        f"p95={related_p95:.3f}  max={max(related):.3f}"
    )
    print(
        f"unrelated min={min(unrelated):.3f}  p05={unrelated_p05:.3f}  "
        f"median={statistics.median(unrelated):.3f}  max={max(unrelated):.3f}"
    )
    print(
        f"recommended cutoff: {midpoint:.3f} (midpoint of related p95 / unrelated p05); "
        f"sweep picks {sweep_cutoff:.3f} at balanced accuracy {accuracy:.3f}"
    )
    if max(related) < min(unrelated):
        print(
            f"note: the groups are separated (related max {max(related):.3f} < "
            f"unrelated min {min(unrelated):.3f})"
        )
    else:
        print("warning: the groups overlap — no cutoff separates them perfectly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
