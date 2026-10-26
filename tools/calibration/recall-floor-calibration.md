# Recall floor calibration — `LORE_MEMORY_RECALL_MIN_SIMILARITY` (#199)

Measured 2026-10-14 with the real local model, replacing the 2026-10-13
placeholder (`0.7`, "about 0.76 related / 0.45-0.62 unrelated" from a handful
of short texts).

## Method

- Model: `BAAI/bge-small-en-v1.5` (384-dim), fastembed 0.9.0, running locally —
  no text leaves the machine.
- Data: `tools/calibration/recall-floor-labelled-pairs.json` — 61 pairs
  of (stored memory, user query) in memory style, English and German, labelled
  `related` (acceptable to show for that query) or `unrelated` (must be
  filtered). Same-domain negatives included; one label corrected during the
  run (sourdough hydration vs. water in bread dough is the same subject).
- Tool: `tools/calibration/calibrate_recall_floor.py` — embeds both sides, reports the
  cosine distribution per label and sweeps every observed score as a candidate
  floor, maximising balanced accuracy (equal weight on keeping related hits and
  dropping unrelated padding; ties prefer the higher floor).
- Run (never from a unit test — tests must not download models):

  ```bash
  python tools/calibration/calibrate_recall_floor.py
  python tools/calibration/calibrate_recall_floor.py --json
  ```

## Observed distribution (61 pairs)

| label | n | min | p05 | median | p95 | max |
| --- | --- | --- | --- | --- | --- | --- |
| related | 31 | 0.430 | 0.612 | 0.739 | 0.818 | 0.838 |
| unrelated | 30 | 0.381 | 0.384 | 0.490 | 0.556 | 0.582 |

## Floor candidates

| floor | related kept | unrelated dropped | balanced accuracy |
| --- | --- | --- | --- |
| 0.55 | 94 % | 93 % | 0.934 |
| 0.58 | 94 % | 97 % | 0.951 |
| **0.60** | **94 %** | **100 %** | **0.968** |
| 0.6116 (sweep optimum) | 94 % | 100 % | 0.968 |
| 0.62 | 90 % | 100 % | 0.952 |
| 0.70 (previous default) | 77 % | 100 % | 0.887 |
| 0.75 | 42 % | 100 % | 0.710 |

## Decision

**`DEFAULT_RECALL_MIN_SIMILARITY = 0.6`.** It ties the sweep optimum
(0.968 vs 0.9677 at 0.6116), is a round number with headroom against model
drift, drops every unrelated pair in the set and keeps 94 % of the related
ones. The old `0.7` kept only 77 % of related hits while adding no further
protection against unrelated padding on this set (unrelated max is 0.582).

## Known misses (documented, not fixed here)

- `wie lange bewässern` vs. an English irrigation memory: 0.430.
  bge-small-en-v1.5 is English-only — a German query against an English memory
  scores in the unrelated range, and no floor value can recover it. Candidate
  follow-up with #92: a multilingual model (e.g. bge-m3) and a second
  calibration set in German.
- `when does a commit get merged to main` vs. a paraphrase about the review
  gate: 0.530 — keyword-poor paraphrase. Same miss at any floor ≥ 0.55, so it
  is not a floor-tuning problem; the keyword path (`lore memory recall
  --semantic false` / FTS) still finds it when the wording overlaps.

The duplicate threshold `LORE_SIMILARITY_THRESHOLD` (0.85) is a different
question (near-duplicate reporting) and was **not** recalibrated in this run.
