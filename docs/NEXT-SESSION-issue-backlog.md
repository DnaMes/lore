# Next session prompt — work off the open Lore issue backlog (paste into a fresh Claude Code session)

Written 2026-10-14 at HEAD `136b0e0`. Target: Claude Code (current Claude 5 model) in
`/home/dnames/projects/ai-stack/lore`. Companion prompt for the free-LLM provider work:
`docs/NEXT-SESSION-free-llm-providers.md` (run its steps as "Track L" below).
Ground truth for every issue (status, scope, acceptance, files, proof command, dependencies):
`docs/research/2026-10-14-issue-backlog-digest.md` (index) with the per-issue blocks in
`...-digest-a.md` (#29-#119, #191-#199), `...-digest-b.md` (#151-#170) and `...-digest-c.md`
(#171-#190). Read the matching block before touching an issue.

---

## Objective

Work off all 54 open GitHub issues of `DnaMes/lore` in dependency order: close what is already
done, ship the correctness and safety fixes first, then the query, capture, export and analysis
clusters, and finish with packaging and deployment. Every issue ends in one of three states:
closed with evidence, left open with a precise checklist of what remains, or parked with a
recommended decision in `docs/TODO.md`. Never leave an issue in an unknown state.

## Context (carry forward)

Operating rules (owner policy, do not re-ask):
- The owner delegates decisions. Decide, state the decision in one line, continue. Never hand an
  option menu back. Reply to the owner in German; code, comments, docs, commit messages, issue
  comments and this file's artifacts are English.
- Claude Max weekly limit is nearly exhausted: route implementation to `builder`, tests to
  `test-ops`, commits to `git-ops`, docs/handoff to `doc-ops`, diagnosis to `debugger`. Keep the
  main thread for planning, verification and live checks. One bounded deliverable per lane; a
  delegate hop is capped at 900 s, so split bigger work.
- Commit, push and merge into `main` are standing-authorized, via `git-ops`, per verified unit,
  explicit paths only. Commits to `main` pass `review-gate`; fix BLOCK findings and ship again;
  if one finding survives four rounds report it instead of looping. Never force-push, never
  `--no-verify`. The untracked `.parachute/emergency.md` is not yours.
- Tracker is GitHub issues (`gh ... --repo DnaMes/lore`), not Azure Boards, for this repo.

Repo facts:
- `main` == `github/main`. Conventional Commits. Python 3.11+, ruff, 100-char lines, specific
  exceptions, no `from X import *`, no new third-party dependency without a reason in the commit.
- Test baseline (isolated HOME is mandatory on this host):
  `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts=`
  = 2649 passed, 25 skipped, plus one known failure
  `tests/test_session_summary_card.py::test_absent_summary_renders_at_a_glance_without_provider`
  (also fails on a clean tree; Track L step 9 fixes it). Full suite ~11 minutes: run targeted
  tests per unit and the full suite once per wave.
- Lint: `.venv/bin/ruff format lore tests lore_cli.py && .venv/bin/ruff check lore tests lore_cli.py`.
- Schema: v2 SQLite migrations end at 18 (`lore/storage/schema.py`). Export formats (Markdown,
  standalone HTML, `index.json`) are user-facing: any change is additive only, never breaking
  (`AGENTS.md`: "Break backward compatibility" is forbidden).
- Merged this month and relevant here: `openrouter` and `jev` providers (`lore/llm/`), memory
  lifecycle #197 (`lore improve`, `lore memory ...`, spec
  `docs/superpowers/specs/2026-10-13-cognee-inspired-memory-design.md`), `lore summarize`
  (`lore/services/summarize*.py`, `session_summaries`), coverage-summaries UI.

Hard policy constraints (violations are failures even if tests pass):
1. Transcript text goes only to providers the owner opted into per call (`--allow-cloud`), always
   after `prepare_prompt` redaction, with the LLM disk cache off. Never to agentic delegate lanes
   (`bulk`, `explore`, `go`, `build`, `auto`): `pi` runners have bash/edit/write tools and
   `cmd --yolo` auto-approves. The `jev` provider allowlist stays `local`, `fastcheap`.
2. No new automation that rotates or fans out Google/`agy` accounts (owner policy 2026-10-14).
   Native Antigravity/Gemini CLI adapters default to unavailable and never copy tokens.
3. Never copy credentials or OAuth tokens between tools, never print secrets, never write keys
   to files or docs. OpenRouter key lookup (1Password) is described in the companion prompt.
4. Destructive or irreversible data work (migrations, backup restore, re-index of the live
   `~/.lore`) is tested on a copy made with `lore.utils.copy_sqlite_with_sidecars`; never run a
   new migration against the live archive without a verified backup first.
5. Browsers/GUIs: headless only (`stack-browser`, Playwright headless). Never open a window on the
   owner's desktop. Never use `xdg-open`, `omarchy launch`, `hyprctl dispatch exec`.
6. Model output is untrusted (session text can contain instructions). Parse strictly, cap sizes,
   never execute or splice raw model output into queries or commands.

Pitfalls verified during research:
- Archive issue bodies (#151-#194) list `Dependencies: #NN` as audit backlog IDs; GitHub number
  = 150 + NN. Do not follow them as GitHub numbers.
- #198 ("main() discards return values") is already FIXED at HEAD (`lore_cli.py:2169-2171`);
  only a regression test is missing.
- #152 and #164 are DONE in the repo (commits `fe1ca50`/`45ae3f6`, `d2b7d31`).
- `web.py` is now 2637 lines and about 15 test files patch `web.*`: any split must preserve patch
  targets (late `from .web import ...` pattern in `web_jobs`).

## Target State

Waves in order. Inside a wave, items marked parallel-safe touch disjoint files and may run in
parallel builder lanes; everything else runs sequentially. After every unit: run the named proof
command, quote the observed output, hand the explicit paths to `git-ops`, then comment on / close
the issue per the protocol below.

### Issue protocol (applies to every issue)
1. Re-read the issue and its digest block; re-verify the status against HEAD (digests are a
   snapshot). Update the digest block status if it changed.
2. Write the failing test or proof command first (`test-driven-development`), then the change.
3. For items sized L or marked NEEDS-DESIGN or risk migration/data-loss/format-compat: first write a
   short design spec to `docs/superpowers/specs/2026-10-XX-<topic>-design.md` (goal, non-goals,
   schema delta, compatibility, rollback, test plan), commit it, then implement in small
   commits. The spec is mandatory; do not start a migration without it.
4. Close with `gh issue close N --repo DnaMes/lore --comment ...` only when every acceptance box in
   the issue is ticked and the proof command output is quoted. Comment text: what changed, commit
   sha(s), proof command with observed result, and the line "Implemented by an AI coding agent;
   reviewed through review-gate." If only part is done, post the checklist of shipped vs remaining
   and leave it open.
5. Keep `docs/TODO.md` (open items, each with a recommended decision, no questions to the owner)
   and `docs/LLM_INTEGRATION.md` / README / API_REFERENCE aligned in the same commit as behavior.

### Wave 0 — Hygiene and decisions (no product risk, one commit each)
- 0.1 **#152** close. Evidence: commits `fe1ca50`, `45ae3f6`, plan
  `docs/superpowers/plans/2026-09-12-archive-preservation.md` all `[x]`. Proof:
  `pytest tests/test_storage_writer.py tests/test_services_extraction.py tests/test_index_builder_streaming.py`.
- 0.2 **#164** close. Evidence `d2b7d31`. Proof: `pytest tests/test_extractor_pi.py`.
- 0.3 **#198** add `test_main_propagates_handler_exit_code` to `tests/test_cli_dispatch.py`
  (stub `COMMANDS["list"]` returning 1, assert `SystemExit` code 1; also 0 and None stay exit 0),
  then close with the quoted evidence that the fix landed in `39b5ae84`.
- 0.4 **#196** README lines ~26 and ~107: replace `pip install lore` with install-from-clone
  (`pip install -e .`), keep `import lore`, make no PyPI-availability claim. Add
  `tests/test_install_from_clone.py` that builds a venv in a temp dir, installs the worktree with
  `--no-deps --no-build-isolation` and asserts the five console scripts `lore`, `lore-session`,
  `lore-web`, `lore-mcp`, `lore-web-probe` exist and `--help` exits 0. Proof: `grep -n "pip install lore" README.md`
  prints nothing; the new test passes.
- 0.5 **#33** (epic). Decision: all functional vision items shipped (v2 store, memory MCP/CLI,
  semantic search, memory UI, provenance). Keep a single repository named `lore` (import package
  unchanged). Post a status comment with the digest's checkbox list and close.
- 0.6 **#29** (MCP streamable-http) and **#48** (JSON retirement) are maintainer-deferred (v3).
  Do not implement. Add label `deferred`, post a comment with the recommended decision (29: build
  after #174/#175 stabilise the search API; 48: only after the CI equivalence test between v2 and
  JSON exists, which is sub-task 4 of the issue), and list both in `docs/TODO.md`.

### Wave 1 — Correctness and safety fixes (parallel-safe unless noted)
- 1.1 **#199** (M, none). Work the open bullets in this order; one commit per two bullets:
  scoped-search role check without `LIKE %term%` (`lore/storage/search.py:158-190`, uses FTS
  token/phrase rules); `_has_stored_vectors` restricted to the query's kind/project filters
  (`memory.py:626`); cap the Python memory-vector scan (`memory.py:542-597`) or move it to vec0;
  split `ExtractionReport.failed` into provider-unavailable vs unparsable (`services/knowledge.py`,
  `llm/tasks/knowledge.py`); explicit commit in `save_session_knowledge`; add edge-case tests
  (leading tab, `don't`); decide the 0.7 floor placeholder (`memory.py:69-74`) from a measurement
  and document it. Proof: `pytest -k "memory or snippet or fts_query or scope"`.
- 1.2 **#193** (M, privacy). Add configured public origins (env `LORE_PUBLIC_ORIGINS`, comma list
  of `scheme://host[:port]`) honoured only with the existing trusted-proxy flag; accept configured
  scheme/host/port incl. non-default ports and TLS; reject unrelated, malformed and spoofed
  `X-Forwarded-*` on state-changing routes. Tests: direct, trusted, untrusted, mixed matrix in
  `tests/test_proxy_origin.py` (files `lore/interfaces/web.py:215-269`, `web_utils.py:76-90`).
- 1.3 **#92** (M). Max-distance cutoff in `semantic_search_sessions` (`storage/search.py` 247-316)
  before RRF; calibrate by percentile on the real bge-small distribution measured from a fixture
  (not a bare constant); env override `LORE_SEMANTIC_MAX_DISTANCE`; document in
  `docs/PLAN-hybrid-search.md`. Proof: `pytest -k "semantic or hybrid"`.
- 1.4 **#176** (S). Export one idempotent code-block enhancer from `lore/templates/base.html`,
  call it after lazy-appended pairs load (`session.html` loader), copy raw `textContent` exactly,
  keep CSP-safe (no inline handlers). Proof: `pytest tests/test_render_smoke.py tests/test_session_detail_markup.py`;
  browser-level clipboard check is Wave 7 item 7.3 (NOT VERIFIED until then).
- 1.5 **#112** (M, data-loss; sequential, do not parallelise). Wire `known_source_mtimes` and
  `known_source_parser_versions` into `build_search_index` (`lore/services/extraction.py`
  152-356, currently only a post-parse check at 276-298) so extractors skip unchanged files before
  `open()`. Safety: a wrong skip empties v2 message rows (see #35 comment), so add tests for
  changed-content-same-mtime, parser-version bump, deleted source (tombstone only on explicit
  signal), and run an A/B on a copy of a real archive: index with and without the skip must
  produce identical session ids and message counts. Proof: `grep -rn known_source_mtimes lore/`
  shows `services/extraction.py` plus the A/B equality output.
- 1.6 **#185** (M, privacy). Title generation uses only the explicitly selected provider/model;
  remove the silent llm -> keywords -> first_message fallthrough of cross-provider content
  (`lore/titles/generator.py:119-193`); record provider/model/prompt-version/excerpt/status; the
  local keyword and first-message paths stay as explicit named strategies. Proof:
  `pytest tests/test_web_titles.py tests/test_import_quality_and_titles.py`.

### Wave 2 — Query correctness cluster (sequential: 2.1 -> 2.5), then Track L search steps
Order from the digest: shared data path, #172/#173 before #174, #171 before #175.
- 2.1 **#172** timezone-aware, inclusive-end date parsing in `lore/utils/datetime.py`; reuse in
  CLI (`lore_session_cli.py:25`), web, API, MCP (`mcp_tools/search.py:142`). Tests: DST edges,
  naive legacy timestamps, inclusive end date, all four surfaces agree.
- 2.2 **#173** thread tag/bookmark filters through `search_sessions`, CLI list and MCP; counts,
  pagination and empty states respect tags; durable across rebuild.
- 2.3 **#174** push tool/project/date/tag/role filters into SQL candidate selection and the KNN
  pre-filter before ranking and limit (`storage/search.py:113-147, 225-244`); add the
  "top-N-unfiltered-elsewhere" fixture; invalid filters raise validation errors.
- 2.4 **#171** durable message permalink route and API that loads only the requested window from
  the v2 `messages` table (`web.py:1560-1584` currently materialises the whole session); explicit
  not-found/gone responses.
- 2.5 **#175** per-hit message evidence (seq, excerpt, permalink) and cursor pagination in
  `/api/search`, CLI and MCP; redaction/truncation labelled. Update `docs/API_REFERENCE.md` and
  `tests/test_api_contract.py`.
- Then run Track L steps 4 and 5 (query expansion, rerank): they integrate on top of the new
  filter-before-limit search path and must not be started before 2.3.

### Wave 3 — Capture and identity spine (L, migrations; spec-first, sequential)
Two specs: `...-archive-capture-spine-design.md` (#151, #159, #153, #162) and
`...-session-identity-envelope-design.md` (#154, #155, #160, #161, #163). Commit both specs and
get them through review-gate before any migration code. All schema changes are additive
(migration 19+), idempotent, covered by a "migrate a copy of the real DB and compare counts"
test, and keep legacy rows readable.
- 3.1 **#151** append-only source-artifact/revision store before parsing (identity, checksum,
  size, revision, decode status, attachments); exclude credential/config files; idempotent on
  unchanged sources. New `lore/storage/source_revisions.py`, `tests/test_source_revisions.py`.
- 3.2 **#159** memory-search repair command plus survival tests across full/incremental/empty/
  failed/cancelled syncs (`tests/test_memory_search_survival.py`).
- 3.3 **#153** quality gate becomes a view/query filter; capture every decoded session; counters
  captured/filtered/unsupported/failed in sync output. Default curated list stays identical.
- 3.4 **#162** durable per-source health with seven states and partial-vs-complete; included in
  backups later. Extends `lore/services/discovery.py` and `lore check --deep`.
- 3.5 **#154** versioned canonical message envelope: native id, full tool calls with args/results,
  provider fields, unknown-field extension; legacy migration and round-trip tests
  (`tests/test_contract_fields.py`).
- 3.6 **#155** namespaced session identity, dedupe of byte-identical copies, divergent revisions
  linked, compatibility resolver for old ids. Highest data-loss risk: backup-verified copy test
  mandatory.
- 3.7 **#160** evidence references by canonical identity plus revision, public resolver,
  explicit "unresolved" reporting for legacy `memory_sources.message_id` rowids.
- 3.8 **#161** versioned SQLite-consistent backup bundle (SQLite backup API, manifests,
  checksums), restore into an isolated dir by default, verification of counts/identities/FTS/
  annotations/evidence. New `lore/storage/backup.py`, `tests/test_backup_restore.py`.
- 3.9 **#163** import/merge of portable bundles with provenance; never overwrite local content
  implicitly; conflicts reviewable. New `lore/storage/import_bundle.py`.

### Wave 4 — Extractor fidelity (needs 3.5; sequential)
- 4.1 **#158** availability means readable conversation data; count artifacts/transcripts/
  unsupported separately; preserve undecoded conversations as source artifacts (needs 3.1).
- 4.2 **#156** Codex: persist and export full call args/output/error/truncation via the envelope.
- 4.3 **#157** Claude: preserve non-text blocks, attachments, ids and block order; redaction
  policy explicit and tested (`lore/exporters/redaction.py`).

### Wave 5 — Transcript export and reading experience (parallel-safe with Wave 4 after 3.5)
Order from the digest: 5.1 **#166** canonical tool-call envelope in Markdown/HTML export ->
5.2 **#165** complete, provider-independent export, derived titles/summaries marked ->
5.3 **#169** full Markdown in standalone HTML (offline, sanitised, `nh3`) -> 5.4 **#168** role/
state labels and `data-role`/`data-status` attributes -> 5.5 **#167** view-only normalisation
with idempotency tests -> 5.6 **#170** accessible reading contract with automated checks
(`tests/test_a11y_transcript.py`; 200% zoom and narrow layout via headless Playwright).
All additive: existing Markdown/HTML exports stay readable by current consumers; add a golden-file
test of the old output for a fixture session and assert only additive differences.

### Wave 6 — Analysis pipeline, providers and memory (sequential)
- 6.1 **#197** finish `lore memory review accept|reject` plus the web "Candidates" tab (phase
  3.3) and `lore context [--project P] [--tool T] [--budget 1200]` (phase 4), per the cognee spec.
- 6.2 **#177** hydrate canonical archived messages (not `messages[:20]` / `max_messages=6`) for
  knowledge, titles and stats with revision and coverage metadata; explicit failed/partial state.
- 6.3 **#178** complete chunk coverage or explicit coverage metadata; never mark a truncated run
  complete (`summarize_chunks.py` currently drops middle chunks beyond 8).
- 6.4 **#186** cache keys include source revision, provider identity, parameters, prompt version
  and task; superseded rows preserved; jev disk cache key serialised as JSON (see companion step 1).
- 6.5 **#179** durable job table and non-overwriting artifact retention (migration; spec first);
  replaces in-memory `RELOAD_JOBS`-style job state for summary jobs.
- 6.6 **#180** adapter interface with persisted request fields (provider, model, auth mode,
  timeout, cancellation, execution policy) in `lore/llm/adapters/`; existing gemini/ollama/
  openrouter/jev move behind it without behaviour change.
- 6.7 **#181**, **#182**, **#184** native Codex, Claude Code and Gemini CLI adapters, each with
  native auth only, bounded input, structured output, explicit missing-binary/auth/timeout/
  non-zero-exit states, fake-executable tests, `--allow-cloud` where the CLI is remote.
  Gemini CLI and API stay separate adapters; never copy CLI tokens into API config.
- 6.8 **#183** Antigravity: write a short contract note in `docs/` first; the adapter reports
  unavailable by default and performs no account rotation or fan-out. If the CLI contract cannot
  be confirmed offline, ship only the stub and the note, and leave the issue open with that status.
- 6.9 **#187** extend #197 memory candidates with confidence, generation-request metadata and
  observation/hypothesis typing; reuse `lore/storage/memory.py` statuses, no parallel store.
- 6.10 design-first, one spec each, implement only after the spec is approved by review-gate:
  **#188** workflow metric vocabulary (observed vs computed vs inferred, evidence-linked),
  **#189** reviewed harness-recommendation export (reuse memory/knowledge stores, inactive until
  adopted, no automatic harness modification), **#190** message-level embeddings with a versioned
  DE/EN/mixed/code evaluation set and calibrated thresholds (no model downloads in unit tests;
  embedding provider must respect the local-only policy).

### Wave 7 — Packaging, deployment, release gates (sequential)
- 7.1 **#191** distribution identity. Decision: rename the distribution to `lore-archive`, keep
  `import lore` and all console script names. First check availability live
  (`curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/lore-archive/json`; 404 means free).
  If the check cannot run, write NOT VERIFIED, do not rename, and leave the issue open. Update
  `pyproject.toml`, README, Dockerfile, CI, badges; clean-venv install proof for all five scripts.
- 7.2 **#192** portable `docker-compose.yml`: remove hardcoded private DNS
  (`192.168.50.1/192.168.1.1`), the external `traefik_local` network and Traefik labels from the
  default file; move the homelab variant to `docker-compose.traefik.yml` override; documented
  least-privilege mounts; health check honest. Proof: `docker compose config` on a host without
  the external network, plus `scripts/verify_offline_image.sh` if Docker is available (else NOT VERIFIED).
- 7.3 **#194** repeatable browser fixture: wrap `tools/playwright_e2e_regression.py` as a
  `@pytest.mark.browser` test (skipped when Playwright is not importable), headless only, over a
  synthetic archive; covers search/copy/tags/pagination/themes/resume/delete with keyboard and a
  narrow viewport; fails on console errors, CSP violations, failed requests, dead controls.
  Add a release checklist section to `docs/DEPLOYMENT.md` recording browser version, fixture,
  command and result. This also closes the NOT VERIFIED clipboard check from 1.4.
- 7.4 **#119** last, because everything above touches `web.py`: split into Flask Blueprints
  (api/pages/actions), move `_reload_sessions_index` into `web_jobs`, target `web.py` < ~800 lines.
  One blueprint per commit; the full suite must stay green after each (about 15 test files patch
  `web.*`; keep late-import patch targets working or update the patches in the same commit).

### Track L — free hosted LLM providers (parallel track, independent files)
Execute `docs/NEXT-SESSION-free-llm-providers.md` steps 1, 2, 2b, 3, 9 immediately (no overlap
with the waves above); steps 4 and 5 only after Wave 2.3; steps 6 and 7 when step 3 measurements
justify them; step 8 (docs/handoff) is folded into the end-of-wave documentation below.

## Scope
- Work only in: `lore/`, `lore_cli.py`, `lore_session_cli.py`, `tests/`, `tools/` (browser
  fixture only), `docs/`, `README.md`, `Dockerfile`, `docker-compose*.yml`, `pyproject.toml`
  (only for 7.1), `.github/workflows/` (only for 7.1), `HANDOFF.md`, and the Obsidian project
  folder `~/ObsidianVault/2-Projects/ai-stack/lore/` (via `obsidian-keeper` conventions).
- Do NOT touch: `.env*`, any secret store, the live `~/.lore` archive without a verified backup,
  the `lore/` package name, `.parachute/emergency.md`, other repos or submodule pointers.

## Constraints
- Only make changes directly required by the issue being worked. No drive-by refactors; note them
  as new issues instead (title, one-line scope, recommended decision).
- Backward compatibility of export formats, CLI flags and MCP tool schemas is mandatory; changes
  are additive. Deprecations get a release note and a test.
- Each commit is one logical change, imperative Conventional Commit, issue number in the subject
  or body (`fix(search): ... (#174)`).
- Do not weaken or delete an existing test to make a change pass; fix the cause.

## Acceptance Criteria
- [ ] Wave 0 done: #152, #164, #198, #196, #33 closed with evidence; #29 and #48 labelled
      `deferred` with a recommended decision in `docs/TODO.md`.
- [ ] After each wave: targeted proof commands green, full suite = baseline passed count plus the
      new tests, only the one known failure (until Track L step 9 fixes it), ruff clean.
- [ ] Every closed issue has a comment with commit sha, quoted proof output and the AI-disclosure line.
- [ ] No transcript text reaches a cloud provider without `--allow-cloud`; no transcript reaches
      an agentic lane; asserted by tests added in the unit that introduces a provider/adapter.
- [ ] Existing Markdown/HTML/JSON exports still pass their old golden tests unchanged.
- [ ] `docs/TODO.md`, `HANDOFF.md` (newest section on top), README/API_REFERENCE and the digest
      statuses match reality at the end of each wave.
- [ ] Final report lists each issue with: state (closed / partial / parked), commit shas, proof
      command and observed output, or NOT VERIFIED.

## Action Boundaries
- Proceed with reversible in-scope inspection, edits, tests, commits (via `git-ops`) and issue
  comments/closures that meet the protocol.
- Stop and report (never a menu to the owner) when: the same review-gate finding survives four
  rounds; a migration would run against live data without a verified backup; a step would spend
  metered credit beyond the documented daily budgets; a policy constraint above would be violated;
  the A/B equality check in 1.5 or the migration-copy tests in Wave 3 show any data difference.
  After stopping on one item, continue with the next independent item.

## Progress Evidence
After each unit output one line:
`✅ #N <what changed> | proof: <command> -> <observed result> | commit <sha> | issue: closed|partial|parked`
At each wave end output a table of the wave's issues with the same fields plus the full-suite
result. Ground every claim in a tool result.

## Session Strategy
New session. Read `AGENTS.md`, the top of `HANDOFF.md`, this file and the digest first. Run Wave 0
and Track L (steps 1, 2, 2b, 3, 9) first; they are small and unblock a clean baseline. Delegate
bounded, independent items to `builder` lanes in parallel only when their file sets are disjoint
(Wave 1 items, Wave 4 with Wave 5); keep sequential waves sequential. Use `git-ops` for every
commit and `doc-ops` for HANDOFF/Obsidian/TODO upkeep at each wave end. If context grows large,
hand off with the `context-parachute` workflow at a wave boundary, not mid-wave.

This prompt is for an agentic tool with real system access. Review the scope locks, forbidden
actions, and stop conditions before pasting. Confirm file paths, directories, and permissions match
the actual project.
