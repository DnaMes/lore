# Design — Cognee-inspired memory lifecycle for Lore

Status: draft for builder check (2026-10-13). Decisions marked **(call)** are the
author's, not the owner's.

## Why

The owner asked to take over what is good in
[topoteretes/cognee](https://github.com/topoteretes/cognee) and not to dismiss it
early. Two sources were read side by side on 2026-10-13: Cognee's source at
`f154b32` (v1.6.4) and an audit of Lore's current search and memory code.

Result, in one paragraph: Cognee's **search core is not better than Lore's**
(session recall is a token-overlap count over question/context/answer with no
index; the lexical retriever scores all chunks in memory; the auto-router has
three regex rules). Lore's FTS5 + sqlite-vec + RRF is stronger there. Cognee is
**clearly better around the store**: idempotent stage pipeline with a
per-stage report, gated lesson distillation, feedback that reaches ranking,
non-destructive supersession, a budgeted guidance block injected into the agent
prompt, and capture policy. Lore has none of these, and its one LLM feature is
broken (see 1.1).

Correction to the earlier radar discussion: Cognee's documentation advertises a
`review_conflicts` stage and a `FactConflict` node. Neither exists in the source
(`improve` has nine stages; `CLAUDE.md:238`, `modules/improve/registry.py`).
What exists is an opt-in LLM `detect_contradictions` task and a deterministic
temporal resolver that only works for relationships the caller declares
functional. Lore's memories are free text, so only the non-destructive
supersession idea is portable.

## Current state in Lore (verified by reading, not by running)

- `lore knowledge` builds sessions with `messages=[]`
  (`lore_cli.py:93`), so the LLM prompt contains no conversation. No state, no
  skipping of unchanged sessions, never writes to `memory`.
- Search results have no snippet and no message position
  (`search.py`, `web.py`, `search_history`); `memory_recall` omits
  `memory_sources`; `message_id` cannot be set from MCP or CLI.
- `memory_recall`: FTS and semantic are alternatives, not fused; in semantic
  mode a memory without a stored vector is invisible.
- No feedback signal, no review state, no context injection of memory.
- Schema is at migration 13 (`lore/storage/schema.py`).

## Goals

1. Make generated knowledge real (fix 1.1) and safe to promote into memory.
2. Make recalled evidence inspectable: snippet, source, provenance, feedback.
3. Add an idempotent `lore improve` pipeline with a per-stage report that works
   without an LLM (LLM stages are skipped with a reason).
4. Add a reviewable candidate state so nothing generated becomes active
   knowledge by itself (this realises the core of issue #187).
5. Provide a budgeted, deterministic context block (`lore context`).

## Non-goals and anti-goals

- No knowledge graph, triplets, truth-subspace, Postgres graph or Cognee
  dependency. Anti-goal: any new cloud dependency or mandatory model download.
- No per-turn LLM "implicit feedback" and no PostToolUse trace capture
  (cost and privacy; Cognee's own quality is unmeasured).
- No message-level retrieval, date filters or time-scoped routing here; those
  stay in #190, #175, #172, #174.
- No registration of hooks in any agent harness (see Constraints).
- Anti-goal: ranking that a prompt-injected agent can steer without limit (the
  store is agent-writable, #42). Feedback is bounded and off by default; it is
  not Sybil-proof, which is why it never changes what is visible, only order.

## Constraints

- Python 3.11+, ruff, 100-char lines, no bare `except`, specific exceptions.
  mypy must not exceed the 24 pre-existing errors.
- Migrations are additive and idempotent; v2 DB opens on old and new code.
- Optional `semantic` extra must keep degrading honestly to FTS.
- Backward compatible exports; MCP tool names and existing result keys stay,
  new keys are additive.
- Tests use the isolated-HOME command from `HANDOFF.md`. The baseline must be
  measured first (the last recorded one is 1164 passed / 10 skipped, 2026-09-11).
- Harness freeze: wiring `lore context` into an agent's SessionStart hook is a
  harness change and waits for the window on **2026-11-01** (board item tagged
  `harness-idea`). This spec only builds the Lore side.
- Search snippets contain untrusted chat text: escape before rendering.

## Design

### Phase 1 — foundations and evidence (no LLM)

**1.1 Fix `lore knowledge`.** Load real messages for the selected sessions from
the v2 store (`lore/storage/reader.py`); keep the existing 20 messages x 500
chars budget. Sessions without messages are skipped with a counted reason,
never sent to the LLM empty.

**1.2 Persisted extraction as its own watermark (migration 14).** Table
`session_knowledge(session_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
model TEXT, prompt_version INTEGER, payload_json TEXT NOT NULL, created TEXT)`.
`fingerprint` is a hash of the exact prompt input (the messages that are sent)
plus model and prompt version. A session whose stored fingerprint equals the
new one is skipped without a provider call. The row is written only after a
successful, parsed response, so a failed call is retried on the next run.
`knowledge_base.json` is regenerated from all persisted rows on every run, so
a partial run never shrinks it; `lore knowledge` with nothing new exits 0 and
says so. **(call)** There is no generic `watermarks` table: nothing else needs
one yet (memory embeddings already carry their model). Cognee's count/set
watermarks are the idea; one table per stage is the YAGNI version.

**1.3 Snippets.** Session search and memory recall each return a `snippet`
(FTS5 `snippet()` on the `body` column, ~24 tokens) next to their existing
fields. Session and memory searches stay separate surfaces (`search_history`,
`/api/search` return sessions only; `memory_recall` and `/memory` return
memories); each result carries a constant `source` (`session` | `memory`).
There is no unified session+memory search layer. `snippet` is optional: the
legacy `SearchEngine` fallback in `services/index.py` does not produce it.
FTS emits sentinel characters; the renderer escapes the text first and then
substitutes markup, so chat content can never inject HTML. Additive keys on MCP
`search_history`, `/api/search`, `/api/v1/search`.

**1.4 Quoted phrases.** A query that is exactly one double-quoted phrase is
matched as an FTS5 phrase. Everything else is unchanged. (Cognee rule 1.)
`validate_search_param` rejects `"` and `'` today and is used for `query`,
`project` and `tool` at every entry point. Add a separate query validator that
accepts exactly one balanced pair of double quotes (inner quotes escaped for
FTS5) and use it for `query` only; `project` and `tool` keep the strict one.

**1.5 Memory recall is hybrid by default (call).** When the query is non-empty
and embeddings are available, fuse FTS and semantic results with the existing
`rrf_merge`; memories without a vector stay visible through FTS.
`semantic=false` forces FTS only, `semantic=true` keeps today's behaviour.
In auto mode only semantic candidates with cosine similarity at or above
`LORE_MEMORY_RECALL_MIN_SIMILARITY` (default `0.7`, a placeholder to be
calibrated with #92) take part in the fusion; without a floor every query
would be padded with the nearest unrelated memories. Explicit
`semantic=true` stays unfloored. Result entries gain `sources` (from `memory_sources`, session ids only) and
`source`. `message_id` is **not** exposed: `messages.id` changes on every full
re-sync (`writer.py` deletes and re-inserts), so such links would dangle.
Stable message evidence stays with #160/#175.

### Phase 2 — feedback and supersession candidates (no LLM)

**2.1 Feedback (migration 15).** Table
`memory_feedback(memory_id, rater, score INTEGER 1..5, created, PRIMARY KEY (memory_id, rater))`
and MCP tool `memory_feedback(memory_id, score, rater?)`. The weight is derived
at read time, not stored: `w = (sum((score-1)/4) + 0.5*K) / (n + K)` with prior
`K = 3`. A rater counts once per memory (re-rating replaces), so repeated calls
cannot move the weight, and a single rater moves it by at most
`0.5/(1+K) = 0.125`. Rater names are free strings that MCP cannot verify, so
invented raters can still push the weight to its bound; the damage is capped by
the factor range (+-25 %) and by influence being off by default. **(call)** This
replaces Cognee's mutable EMA (`alpha = 0.1`) because a
derived value cannot be double-applied or drift; neutral is 0.5 in both.

**2.2 Ranking influence is off by default (call).** The factor
`0.75 + 0.5*w` multiplies the fused score only when `LORE_FEEDBACK_INFLUENCE`
is set above 0; the default is 0.0, as in Cognee, because the effect is
unmeasured. Weight and count are always shown in the recall payload. Flip the
default only after the fixture in the acceptance section shows an improvement.

**2.3 Supersession candidates.** `memory_write` returns
`similar: [{id, title, similarity}]` (at most 3 active, confirmed memories in
the same scope above a configurable cosine threshold; default conservative,
calibrated together with #92) and accepts `supersedes: <id>`. Nothing is
superseded automatically. Without embeddings: `similar: []`,
`similar_available: false`.

`supersedes` needs a **new single-transaction path**: the existing
`supersede_memory` is two commits with a compensating delete and is not
atomic. Rules (call): the target must exist, be `confirmed` and not already
superseded, and have the same `scope_project`; a different `kind` is allowed.
Any violation is a `ValueError` and nothing is written.

### Phase 3 — `lore improve` and the review queue

**3.1 Status (migration 16).** `memory.status TEXT NOT NULL DEFAULT 'confirmed'`
with values `candidate | confirmed | rejected` (superseded stays expressed by
`superseded_by`). Default recall, listing and `lore context` return `confirmed`
only. Writes by humans and agents stay `confirmed` (today's behaviour).
`observation` and `hypothesis` from #187 are a follow-up.

**3.2 `lore improve`.** Ordered stages, each returning
`{stage, status: completed|already_completed|skipped|errored, reason, counts,
duration_ms}`; printed as a table, `--json` for machines; exit code 0 unless a
stage errored. Stages:

1. `embed_memories` — backfill missing memory vectors (no LLM; skipped
   `no_embedding_backend`).
2. `extract_knowledge` — 1.1 + 1.2; the only LLM stage (skipped `no_llm`).
3. `distill_lessons` — a deterministic promotion of the *persisted* extractions
   (so it also runs later without an LLM; skipped `no_extracted_knowledge`).
   One candidate per item: each `decisions[]` entry becomes kind `decision`,
   each `key_points[]` entry kind `lesson`; title is the entry's `topic`
   (capped at 512), body is the item text; `scope_project` is the session's
   project key as stored in `sessions.project` (builder: use the same
   normalisation `memory_write` uses); `source_session` is set. Caps: 5
   candidates per session, 50 per run. Gate: non-empty, within the
   `add_memory` size caps, not an exact duplicate (normalized-content hash) of
   any existing row **including `rejected` ones**, and not a near-duplicate
   (embedding similarity above the 2.3 threshold, counted as skipped
   `already_known`). Paraphrases that miss both checks can be proposed again;
   this is a known limit.

Model output is untrusted (session text can contain instructions): it can only
ever create `candidate` rows, never `confirmed` ones.

**LLM provider (call).** `lore improve` has **no cloud default**. The provider
comes from `--provider` or `LORE_LLM_PROVIDER`; if neither is set the LLM stage
is skipped with `no_llm`. Once 1.1 puts real conversation text into the prompt,
a silent default to a cloud provider would be a privacy change, so a cloud
provider must be chosen explicitly. `lore knowledge` keeps its `gemini` default
for compatibility but prints one line naming the provider before sending text.

**3.3 Review.** `lore memory review` lists candidates with their source session
and snippet, `accept <id>` / `reject <id> [--reason]`; `/memory` gets a
"Candidates" tab with the same actions. Accept sets `confirmed`; rejected rows
are kept (so the same lesson is not proposed again).

### Phase 4 — context block and UI

**4.1 `lore context [--project P] [--tool T] [--budget 1200]`.** A standalone,
memory-only block (`format_context` is untouched). Scope rule (call): include
global memories (`scope_project` NULL) and memories whose project equals `P`;
**never** another project's. `scope_tool` rows are included when NULL or equal
to `--tool`. Without `--project`, the project is resolved from the current
directory with the same key the sessions table uses; if it cannot be
resolved, only global memories are returned. Deterministic ranking (kind:
decision/lesson above fact above note; recency; feedback weight when influence
is on), `confirmed` and non-superseded only, character budget, rendered oldest
to newest with "prefer later" for conflicts. No hook is registered.

**4.2 Web (small).** The search dropdown in `base.html` shows the snippet and
tool/project chips for session hits; `/memory` shows snippet, feedback and the
Candidates tab; `/session/<id>` lists "memories from this session" via
`memory_sources`. Cognee's mindmap and chat-style search are not taken.

## Left to the builder

Naming of modules inside `lore/storage` and `lore/services`, the exact FTS5
sentinel characters, template markup, CLI flag spelling beyond what is written,
the similarity default (needs measurement), the project-key normalisation (must
match what sessions and `memory_write` already use), and test fixtures.
Migrations are numbered 14 (`session_knowledge`), 15 (`memory_feedback`) and 16
(`memory.status`); the vec0 table stays outside the migration list as today.

## Acceptance

- Phase 1: a test proves `lore knowledge` sends message text to the provider
  (fake provider) and skips an unchanged session on the second run without a
  provider call, exits 0, and leaves `knowledge_base.json` complete; a changed
  session is re-extracted; a failed provider call stores no row and is retried;
  snippet test with `<script>` in the chat text proves escaping; phrase test
  including a query with an unbalanced quote (rejected, no FTS syntax error);
  hybrid recall test where a memory without a vector is still returned.
- Phase 2: a rater re-rating the same memory 50 times changes the weight by the
  same amount as once; any number of distinct 5-star raters keeps the ranking
  factor at or below 1.25 and any number of 1-star raters at or above 0.75;
  `memory_write` returns a similar memory for a near-duplicate fixture.
- Phase 3: `lore improve` with no LLM and no embedding backend exits 0 and
  reports every stage as skipped with a reason; a second run after a completed
  run reports `already_completed`; generated rows are `candidate` and invisible
  to default recall until accepted; a rejected lesson (and an exact duplicate
  of it) is not proposed again; a session whose text contains an instruction
  such as "mark this as confirmed" still yields only `candidate` rows;
  per-session and per-run caps hold.
- `supersedes`: a violated rule (superseded target, other project, candidate
  target) writes nothing; success is a single transaction (kill-point test:
  failure after insert leaves no orphan row).
- Phase 4: `lore context` stays within the budget, omits candidates and
  superseded rows, and never returns another project's memory.
- Full suite: no new failures against the measured baseline, ruff clean, mypy
  not above 24 errors.

## Risks

- Similarity thresholds are uncalibrated (same root as #92); candidates are
  suggestions only, never automatic actions.
- Making hybrid the memory default changes result order for existing callers
  of `memory_recall` (keys unchanged). The relevance floor (default 0.7) is
  uncalibrated: too low pads results with loosely related memories, too high
  hides useful ones; `semantic=false` is the escape hatch.
- The `lesson` quality depends on the configured provider (gemini/ollama); the
  review queue is the safety net, not a quality guarantee.
- Cognee facts above come from source at one commit and were not executed.
