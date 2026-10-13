# Design — Tool coverage, AI session summaries and a new session view

Status: draft for builder check (2026-10-14). Decisions marked **(call)** are the
author's, not the owner's. Follows `2026-10-13-cognee-inspired-memory-design.md`
(issue #197, phases 1-2 shipped).

## Why

The owner wants Lore to become a product that covers all AI coding tools, shows
sessions in a form that is pleasant to read, and can summarise sessions with an
AI. Five read-only research passes on 2026-10-14 produced the evidence; they are
committed under `docs/research/` and are the source for every concrete schema,
path and number below:

- `2026-10-14-tool-formats-dossier.md`: where and how 25+ tools store sessions,
  drift findings for the extractors Lore has, field-level schemas for Pi,
  Command Code and Hermes.
- `2026-10-14-session-summaries-design.md`: summary pipeline, data model,
  prompts, validation, evaluation.
- `2026-10-14-session-ui-redesign.md` plus `docs/research/mocks/`: measured
  problems of the current UI, patterns from prior art, wireframes, order.
- `2026-10-14-oss-landscape.md`, `2026-10-14-community-needs.md`: competitors
  and what users ask for.

## State today (verified on one developer machine, 2026-10-14)

- Lore lists 10 tools. Gemini CLI is reported "available" but yields **0**
  sessions: the extractor globs `session-*.json` and requires 64-char project
  directories, while the tool now writes `session-*.jsonl` into slug
  directories. Antigravity yields 0 (reads only `task.md`; `.pb` is encrypted).
  Codex keeps 63 of 1,246 rollouts (the `MIN_USER_PROMPTS=3` filter drops
  non-interactive `exec` runs) and loses tool output, tokens and model. Cursor
  keeps text only, with one timestamp for every message. Pi, Hermes and Command
  Code are not read at all.
- The session UI has measured defects (dense but wasteful rows, a duplicate
  outline, a destructive button on every row, an escaping bug that breaks
  `data-session-id`, clipped content at 390 px).
- There is no secret redaction anywhere, the Gemini provider keeps a plaintext
  disk cache, and the Ollama provider sets no context size.
- Competitors with the same idea exist and are active (agentsview, cass,
  deja-vu). The name "Lore" collides on PyPI and with two other products.

## Goals

1. Every tool with a local history that is relevant in 2026 is read correctly,
   with drift detected instead of silent zeros.
2. A session carries what the tool recorded: origin (interactive or automated),
   parent/child relations, tree structure, thinking, tool calls with results,
   model and token usage.
3. Summaries: per session first, with citations to message numbers, no
   invented files or commands, nothing sent to a cloud provider without an
   explicit opt-in, and no secret leaving the machine.
4. A session list and detail view that are scannable, with the AI summary where
   it helps and a useful deterministic fallback where it does not exist.
5. The remaining parts of #197 (improve pipeline, review queue, context block).

## Non-goals and anti-goals

- No product packaging, rename, PyPI publication, desktop app or single-binary
  distribution in this program; those are owner decisions (see "Owner decisions").
- No decoding of Antigravity `.pb` files, no JetBrains/Windsurf Cascade/Augment/
  Trae-IDE readers (encrypted or undocumented), no SSH multi-machine sync, no
  native cross-agent session port, no cloud features.
- Anti-goal: an extractor that silently returns nothing after a format change.
  Anti-goal: sending private text to a provider the user did not choose.
  Anti-goal: AI output written to a field that other code treats as source data
  (`session.summary`, titles, memory without review).

## Constraints

- Python 3.11+, ruff, 100-char lines, no bare `except`, mypy not above its
  baseline (24 errors at `139a341`, measure again before changing anything).
- Migrations additive and idempotent; the last migration is 16.
- Extractors read the user's tool data **read-only**, never write, never print
  message content in logs, never touch `auth`/`creds`/token files. SQLite
  sources of other tools are copied (with `-wal`/`-shm`) into a scratch
  directory and read there, because opening the original can create files
  beside it.
- Development never happens in the main checkout: `lore-sync.timer` runs
  `lore_cli.py export --all` from it against the real archive every two hours.
  Work happens in a git worktree; the main checkout stays on `main`.
- Tests use synthetic fixtures built from the documented schemas; they never
  read the real home directory and never use the shared test HOME for manual
  runs. Real-data checks are read-only counts and are reported, not asserted.
- Public repository: no personal paths, names or project slugs in committed
  files or fixtures.
- No network and no model download in unit tests; providers are faked.
- The archive stays faithful to what the tools wrote. **(call)** Redaction
  applies on egress (LLM prompts, exports, MCP-triggered summarisation), not at
  index time, because a redacted archive would silently lose information the
  owner may need (the landscape report suggests index-time redaction; this
  design chooses egress, and egress is where the risk is).

## Design

### A. Contract (one migration, additive)

`UnifiedSession` gains `origin` (`interactive` | `exec` | `cron` | `subagent` |
`unknown`), `parent_session_id`, `relation` (`subagent` | `fork` |
`continuation` | `compaction` | None) and `source_version` (the tool's own
format or CLI version) and `parser_version` (see below). `UnifiedMessage`
gains `kind` (`text` | `injected_context` | `compaction_summary`), `parent_id`
and `active` (False for messages removed from model context, such as Pi
`context_edit` targets and Hermes `active=0`). A message stays one row per
turn: tool calls and results remain inline in `content` in the existing
`[Tool: ...]` text form (search and summaries read it), while a compact
`tool_events_json` (name, status, duration, is_error) and a separate `reasoning`
column carry what the view needs without duplicating arguments or results.
All new fields default to the old behaviour so existing extractors keep working
unchanged. Storage gets the matching columns (migration 17), readers and the
index JSON expose them as additive keys.

Reuse invalidation **(call)**: the writer reuses unchanged sources without
parsing, so enriched fields would stay empty for already indexed sessions.
Every extractor declares a `PARSER_VERSION`; a session indexed with a lower
version is re-extracted even when its source file is unchanged.

Discovery telemetry: each extractor counts files matched, files parsed and
records of unknown type; `lore check` prints a warning when sources exist but
yield zero sessions, naming the glob and the counts.

Noise policy **(call)**: sessions with `origin` `exec`/`cron`/`subagent` are
no longer dropped by `MIN_USER_PROMPTS`; they are stored, tagged, and hidden
from default list and search views and from the LLM stages (`knowledge`,
`improve`, `summarize`) behind an "include automated" switch. Interactive sessions keep the
existing thresholds. Rationale: 95% of Codex rollouts on the reference machine
are automated runs and the owner values completeness of the archive; hiding is
reversible, dropping is not.

### B. Extractors, in this order (details in the formats dossier)

1. Gemini CLI repair (jsonl, `$set`/`$patch`/`$rewindTo` replay, `.project_root`
   resolution, subagent directories, `Part[]` content).
2. Pi (version 3 tree; active branch by `parentId`; `compaction`,
   `context_edit`, `custom_message`; usage and cost; sibling harnesses OMP/Prime
   with the same wire format).
3. Codex enrichment (custom tool calls and outputs, `function_call_output`,
   `token_count`, model from `turn_context`, subagent parent, `origin` from
   `session_meta.source`, `archived_sessions`, optional `.jsonl.zst` when a zstd
   module is importable).
4. Antigravity from `transcript.jsonl` and `conversation_summaries.db`.
5. Command Code (transcripts and checkpoint-only print runs).
6. Hermes `state.db` (per-profile, read-only open, WAL aware).
7. Claude Code enrichment (thinking, model, `ai-title`, compaction and meta
   flags, `cost-state`).
8. Cursor (agent-transcripts JSONL, per-bubble timestamps, model, thinking,
   tools, `composerHeaders`; never copy raw composer JSON because it holds
   encryption keys).
9. Wave 2, each small and fixture-based: Cline/Roo/Kilo (shared task schema),
   OpenCode v2 tables, Goose, Crush, Kimi, Qwen Code, Factory Droid, Zed. Marked
   "not verified on real data" until someone with the tool confirms.

Each new tool gets a `Tool` enum value, alias normalisation, a filter entry in
CLI and web, and one line in the README coverage table.

### C. Safety layer

`lore/utils/redact.py`: `redact(text) -> RedactionResult(text, count, kinds)`
with a documented pattern list (provider key prefixes, `Bearer`, JWT, PEM
blocks, `KEY=`/`TOKEN=`/`password=` pairs, long hex/base64 runs). Used by every
path that sends transcript text to a model. Provider hardening: the Gemini
provider's disk cache is off for transcript-bearing tasks; the Ollama provider
takes an `options` passthrough and the summary task sets `num_ctx`.

### D. Summaries

Implement the design in `2026-10-14-session-summaries-design.md` with these
fixed points: table `session_summaries` (migration 18) keyed like
`session_knowledge` by fingerprint of prompt + `provider/model` + prompt and
compressor versions; citations are `messages.seq`; files, commands and errors
come from a deterministic extractor, never from the model; cloud providers only
with `--allow-cloud`; a dry run prints the expected token cost; MCP exposes
`get_session_summary` read-only and `summarize_session` only when
`LORE_MCP_ALLOW_SUMMARIZE=1` and only with a local provider; summaries never
overwrite `session.summary` or titles.

### E. Session view

Follow `2026-10-14-session-ui-redesign.md` section 11 in its order: bug fixes
and compact rows first, then single-frame code blocks and honest tool status,
day grouping and a sticky compact header, the summary card (AI when present,
deterministic "at a glance" otherwise), tool-group folding with merged view
toggles, mobile sheets and bulk select, in-session find and the peek pane last.
No SPA framework, no inline event handlers, Tailwind utilities only if the
build script is run, otherwise semantic CSS in the existing style block.

### F. Remaining #197

`lore improve` consumes summaries (outcome, decisions, errors) and persisted
extractions as input and writes `candidate` memories with `memory_sources`;
review queue in CLI and web; `lore context`; ratings accepted only for
confirmed memories (review-gate note on phase 2). Details: the 2026-10-13
design, sections 3 and 4.

### G. Small additions with high evidence

Retention warning: `lore check` reports when Claude Code's transcript
retention is the default (30 days) and shows how to raise it; the archive
export keeps what the tool would delete. Per-session token and cost columns
from stored usage with an offline price table. Both read-only.

## Owner decisions (not engineering, recorded in docs/TODO.md)

1. **Product name.** "Lore" collides on PyPI (an unrelated framework owns the
   name), with `getspecstory/lore` and with another product announced on HN.
   Recommended decision: rename before any public launch; this program keeps the
   internal package name `lore` and does not touch branding.
2. **Distribution.** Competitors ship one binary or a desktop app; Lore ships as
   a Python package with a Flask UI. Recommended decision: keep Python for now,
   add a `pipx`-installable entry point and a Docker image once coverage is
   done; do not rewrite.
3. **Windows/macOS.** Paths for those systems come from other projects' docs and
   are unverified. Recommended decision: ship Linux first and mark the others
   "best effort" until someone runs them.

## Acceptance (program level)

- Each extractor: fixture tests built from the dossier schema covering message
  roles, tool calls with results, usage, a malformed line, an unknown record
  type, and a format-version bump; a read-only real-data count reported in the
  PR description.
- `lore check` shows, per tool: sources found, sessions parsed, skipped by
  reason, unknown record types; a stale glob produces a warning.
- Summaries: every cited seq exists, no secret-shaped string in the outbound
  prompt or the stored output, an unchanged session makes zero provider calls,
  a growing session re-runs only its last chunk and the merge, tombstoned
  sessions are never sent and their rows are removed.
- UI: no content clipped at 390 px, `data-session-id` equals the session id exactly after HTML parsing, delete only
  behind a menu or selection, keyboard operable list and detail.
- Full suite: no new failures against the measured baseline, ruff clean, mypy
  not above baseline.

## Risks

- Formats change monthly; every extractor is therefore tested against a
  fixture that includes a version field and tolerates unknown record types.
- Hiding automated sessions by default changes what users see after the
  upgrade; a one-time notice and the switch mitigate it.
- A local small model may produce weak JSON; the validator and one repair retry
  are the safety net, not a quality guarantee.
- Growth of the index (Codex `exec` runs, tree messages) affects reload time and
  memory; measure on the reference archive before and after each extractor.
- Research claims marked NV in the dossier stay unverified until tested.
