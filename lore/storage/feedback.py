"""Memory feedback: 1-5 ratings and the bounded weight derived from them (#197).

Ratings live in ``memory_feedback`` (migration 15), one row per (memory, rater).
The weight is computed at read time from the rows and never stored. Rater
names are unverified labels, so the effect of feedback on ranking is bounded
(see :func:`ranking_factor`) and off unless ``LORE_FEEDBACK_INFLUENCE`` is set.
"""

from __future__ import annotations

import math
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Tuple

from .schema import open_v2_connection
from .writer import v2_db_path

MAX_RATER_LEN = 64

FEEDBACK_INFLUENCE_ENV = "LORE_FEEDBACK_INFLUENCE"
DEFAULT_FEEDBACK_INFLUENCE = 0.0

# The weight is a mean of normalised scores pulled toward the neutral 0.5 by
# this many virtual neutral votes, so one or two ratings cannot dominate.
_PRIOR_VOTES = 3
_NEUTRAL = 0.5


# SQLite integers are signed 64-bit; a larger Python int raises OverflowError on bind.
_MAX_ID = 2**63 - 1


def _id_in_range(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_ID


def _check_id(value: object, name: str) -> None:
    """Raise ``ValueError`` unless ``value`` is an int in 1..2**63-1."""
    if not _id_in_range(value):
        raise ValueError(f"{name} must be an integer from 1 to {_MAX_ID}")


def weight_from_scores(scores: Iterable[int]) -> Tuple[float, int]:
    """Return ``(weight, count)`` for raw 1-5 scores; ``(0.5, 0)`` when empty."""
    normalised = [(s - 1) / 4 for s in scores]
    count = len(normalised)
    weight = (sum(normalised) + _NEUTRAL * _PRIOR_VOTES) / (count + _PRIOR_VOTES)
    return weight, count


def ranking_factor(w: float, influence: float) -> float:
    """Multiplier for a ranking score: 1.0 at influence 0, within 0.75-1.25 at 1."""
    return 1 + influence * ((0.75 + 0.5 * w) - 1)


def feedback_influence() -> float:
    """The ranking influence: env value clamped to [0, 1], default 0 if invalid."""
    raw = os.environ.get(FEEDBACK_INFLUENCE_ENV, "").strip()
    if not raw:
        return DEFAULT_FEEDBACK_INFLUENCE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_FEEDBACK_INFLUENCE
    if math.isnan(value):
        return DEFAULT_FEEDBACK_INFLUENCE
    return min(1.0, max(0.0, value))


def _connect(output_dir: Path) -> sqlite3.Connection:
    conn = open_v2_connection(v2_db_path(output_dir))
    conn.row_factory = sqlite3.Row
    return conn


def record_feedback(output_dir: Path, memory_id: int, rater: str, score: int) -> None:
    """Rate a memory 1-5 as ``rater``; a second rating by the same rater replaces.

    Raises:
        ValueError: if the score is not an integer 1-5, the rater is empty, over
            64 chars or holds control characters, the memory does not exist, or
            its status is not ``confirmed``.
    """
    _check_id(memory_id, "memory_id")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise ValueError("score must be an integer from 1 to 5")
    rater = (rater or "").strip()
    if not rater:
        raise ValueError("rater must not be empty")
    if len(rater) > MAX_RATER_LEN:
        raise ValueError(f"rater exceeds {MAX_RATER_LEN} chars")
    if any(unicodedata.category(c).startswith("C") for c in rater):
        raise ValueError("rater must not contain control characters")

    conn = _connect(output_dir)
    try:
        try:
            cur = conn.execute(
                """
                INSERT INTO memory_feedback (memory_id, rater, score, created)
                SELECT id, ?, ?, ? FROM memory WHERE id = ? AND status = 'confirmed'
                ON CONFLICT(memory_id, rater) DO UPDATE SET
                    score = excluded.score, created = excluded.created
                """,
                (rater, score, datetime.now(timezone.utc).isoformat(timespec="seconds"), memory_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"could not record feedback: {exc}") from exc
        if cur.rowcount < 1:
            row = conn.execute("SELECT status FROM memory WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise ValueError(f"memory {memory_id} does not exist")
            raise ValueError(f"memory {memory_id} is {row['status']}, not confirmed")
    finally:
        conn.close()


def feedback_weight(output_dir: Path, memory_id: int) -> Tuple[float, int]:
    """Return ``(weight, count)`` for a memory; ``(0.5, 0)`` when it has no ratings."""
    if not _id_in_range(memory_id):
        return weight_from_scores([])
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            "SELECT score FROM memory_feedback WHERE memory_id = ?", (memory_id,)
        ).fetchall()
    finally:
        conn.close()
    return weight_from_scores(r["score"] for r in rows)
