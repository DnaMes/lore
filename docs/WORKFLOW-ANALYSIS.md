# Workflow analysis: metric vocabulary and evidence contract

Issue: [#188](https://github.com/DnaMes/lore/issues/188).
Implementation: `lore/services/workflow_metrics.py`, surfaced by `lore metrics`.

This document is the contract. A number produced by `lore metrics` is only
meaningful together with its **class**, its **source fields** and its
**coverage**; every result carries all four.

## The three classes

| Class | Meaning | Rule |
|-------|---------|------|
| `observed` | The value is a field that exists in the archive, read as-is (a stored `origin`, a stored tool-event status). | No arithmetic, no judgement. |
| `computed` | Derived deterministically from observed fields (a mean, a median, a ratio). | Recompute it from the same archive and you get the same number. |
| `inferred` | A heuristic judgement about intent or outcome. | Only produced on explicit request (`--include-inferred`), always labelled `inferred`, never added into an observed or computed number. |

`MetricResult.__add__` enforces the last rule in code: combining two
`MetricResult`s of different classes raises `ValueError`. Same-class sums are
allowed for counts only.

### Coverage

Every result reports `covered_sessions` / `total_sessions` and `coverage`
(`covered / total`, rounded to 4 decimals; `0.0` when there are no sessions).

**A missing source field is never reported as `0`.** When a field is absent on
every session, `value` is `null`, `coverage` is `0.0`, and the first caveat
starts with `no data:` and names the field. A session whose field is missing is
excluded from the calculation and named in a caveat — it is never silently
folded into a zero or a mean.

## The metric set

Fixed, small, and deliberately not extensible by configuration.

### `sessions_by_origin` — `observed`

- **Definition:** count of sessions per stored `origin` (`interactive`, `exec`,
  `cron`, `subagent`).
- **Source fields:** `UnifiedSession.origin` (stored as `sessions.origin`,
  schema migration 17).
- **Coverage limits:** `origin = 'unknown'` means the extractor did not record
  one; those sessions are excluded from the breakdown and reported in a caveat.
  `unknown` is **not** a synonym for `interactive`. When every session is
  `unknown`, the value is `null` (coverage 0), not an empty object and not zero.
- **Reproducible:** yes, from the archive.

### `prompts_per_session` — `computed`

- **Definition:** arithmetic mean of user prompts per session.
- **Source fields:** `UnifiedMessage.role`, `UnifiedMessage.kind`,
  `UnifiedMessage.content` (via `UnifiedSession.user_prompt_count`).
- **Prompt** = a message with `role = user`, `kind = 'text'` and non-empty
  content. Injected context and compaction summaries are excluded by
  construction.
- **Coverage limits:** sessions with no stored messages are excluded (they carry
  no evidence either way) and counted in a caveat. A session with messages but
  no prompts is a genuine `0` and is included.
- **Reproducible:** yes.

### `messages_per_session` — `computed`

- **Definition:** arithmetic mean of stored message rows per session.
- **Source fields:** `UnifiedSession.messages`.
- **Coverage limits:** counts every stored row, including injected context and
  compaction summaries, so it is not equal to "turns". Sessions with no stored
  messages are excluded and reported.
- **Reproducible:** yes.

### `tool_call_failure_rate` — `observed`

- **Definition:** failed classifiable tool events / classifiable tool events, for
  the whole archive.
- **Source fields:** `UnifiedMessage.tool_events[].status`,
  `UnifiedMessage.tool_events[].is_error` (stored as `messages.tool_events_json`).
- **Classification:** `is_error = true`, or a status in
  `error|failed|failure|fail`, counts as failed. A status in
  `success|succeeded|ok|completed|complete|passed` counts as succeeded. Anything
  else is **unclassified** and excluded — never assumed successful.
- **Coverage limits:** `covered_sessions` counts sessions with at least one
  classifiable event. The compact event rows carry no arguments or results, so
  this is a status count, not a taxonomy of failures. Extractors that never
  record statuses leave coverage at `0` with a `no data:` caveat.
- **Reproducible:** yes.

### `median_session_duration_seconds` — `computed`

- **Definition:** median wall-clock span from `created_at` to `last_updated`, in
  seconds.
- **Source fields:** `UnifiedSession.created_at`, `UnifiedSession.last_updated`.
- **Coverage limits:** the span includes idle time between turns; it is elapsed
  wall clock, not time-on-task. Timestamps are compared after dropping `tzinfo`
  (the archive's storage frame). A pair where `last_updated` precedes
  `created_at`, or a missing endpoint, is excluded and counted in a caveat — the
  interval is not clamped to zero.
- **Reproducible:** yes.

### `inferred_outcomes` — `inferred` (explicit request only)

- **Definition:** tally of `succeeded` / `failed` / `unclassified` sessions from
  a keyword heuristic over the final assistant text message.
- **Source fields:** `UnifiedMessage.role`, `UnifiedMessage.kind`,
  `UnifiedMessage.content`.
- **Heuristic:** the message must contain a success marker (`done`, `complete`,
  `completed`, `fixed`, `passing`, `resolved`, `implemented`) or a failure
  marker (`error`, `failed`, `cannot`, `unable`, `blocked`, `not working`) as a
  whole word. Both, or neither, means `unclassified` — the classifier does not
  guess.
- **Coverage limits:** this is a judgement about what the user and model meant,
  not an observed outcome. It is never produced unless `--include-inferred` is
  passed, and it can never be summed into an observed or computed number.
- **Reproducible:** the heuristic is deterministic, so the same archive yields
  the same tally — but reproducing it does not make it true.

## What this does **not** claim

- **No causation.** Sessions are correlated with nothing here: there is no
  git/session joining, no "this workflow made the change faster". A ratio of
  failed tool calls is not an explanation of quality.
- **No product score.** No composite index, no weighting, no ranking. A weighted
  single number would mix classes and hide coverage.
- **No cross-tool normalisation beyond the field names above.** Two tools that
  populate `tool_events` differently have different coverage, and the coverage
  number is the honest way to say so.
- **No inference without a flag.** Inferred metrics require an explicit request
  and stay labelled in text and JSON output alike.

## Reproducing

```bash
lore metrics                      # human-readable, grouped by class
lore metrics --json               # machine-readable, one object per metric
lore metrics --include-inferred   # adds the inferred_outcomes block
lore metrics --include-automated  # exec/cron/subagent sessions included
```

The command is read-only and local: it extracts sessions, computes, prints. It
writes no file, opens no socket, and needs no index.

The deterministic fixtures and the hand-derived expectations live in
`tests/test_workflow_metrics.py` (exact numbers, an absent source field, and the
inferred/observed separation).
