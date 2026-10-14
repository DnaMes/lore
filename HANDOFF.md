# HANDOFF — Lore

## Current checkpoint — 2026-10-14 (backlog prompt: Wave 1 complete)

Continued `docs/NEXT-SESSION-issue-backlog.md` in the same session: Wave 1 of the issue backlog.
Every unit below is committed, `review-gate` APPROVE, on remote `main` (tip `f3a65bc` before the
docs close-out).

- **Closed with evidence comments:** #199 (all seven follow-ups: FTS-true scoped role check,
  filtered + bounded vector scan, failure-bucket report, explicit commit, validator edges, recall
  floor calibrated to **0.6** on 61 labelled pairs), #193 (`LORE_PUBLIC_ORIGINS` + forwarded-header
  validation, 25 new tests), #92 (semantic distance cutoff **0.88**, percentile-derived, before RRF
  fusion). Open count 49 → **44**.
- **#185 closed** — titles route through the explicitly selected provider (`local` default,
  `gemini-cli` only when chosen), never switch provider on failure, and record
  provider/model/prompt-version/excerpt/status in an owner-only cache. Deliberate privacy change: a
  SMART/AUTO run no longer sends excerpts to the Gemini CLI just because it is installed.
- **#176 left OPEN on purpose** (implementation shipped in `3a204dd`): one exported idempotent code
  enhancer (`window.loreEnhanceCodeBlocks`) used by the lazy loader, exact `el.textContent` clipboard
  payload, snapshots regenerated. Remaining: the browser-level clipboard/keyboard proof, NOT
  VERIFIED until Wave 7 / #194.
- **#112 (data-loss risk) done and committed** (`c0b1271`): `build_search_index` hands the
  extractors source fingerprints (mtime + minimum parser version; an ambiguous mtime forces a
  re-parse) so an unchanged source is skipped *before* it is opened. A skipped session keeps its
  prior JSON entry, its v2 message rows and its FTS row; a deleted source keeps its entry without a
  tombstone; `incremental=False` never skips. Six tests in `tests/test_incremental_source_skip.py`.
- **Full suite: 2753 passed, 25 skipped, 0 failed** (19m44s; slowed by the concurrent A/B), ruff
  clean.

A/B method (`/tmp/lore_ab.py`, `/tmp/lore_ab_b.py`): both arms start from an identical copy of the
live archive (`index.json` + `index_v2.sqlite` via `copy_sqlite_with_sidecars`); `~/.lore` itself is
only read. Arm A = `incremental=True` (skip active) over all tools: `elapsed=2839.2s, sessions=5850,
messages=685183, errors=0`. Arm B = `incremental=False` re-parsing **claude-code** (3193 sessions,
the largest skip beneficiary): identical id set, 3192/3193 identical message counts — the single
difference is session `539ee2ae…`, whose id exists in two project copies (`-p15` and `-gdrive`)
that were both being appended during the window (mtimes 18:20:42 and 18:21:21), so the extractor's
newest-copy rule picked a different winner per run; the later one (2142 messages) is exactly what
the live archive holds. All other A-vs-B differences (126 ids, 15 counts) sit in tools B did not
re-parse and compare the two copies against each other — the live archive itself changed between
the 18:12 and 20:45 copies. Fingerprints check out on the real archive: 397 of 400 sampled
`index.json` entries carry exactly the on-disk `st_mtime_ns`, 3 sources are gone.

Next: finish the A/B, close #112 with its equality output, then Wave 2 (#172 → #173 → #174 → #171
→ #175, sequential) and Track L steps 4-5 only after 2.3.

## Current checkpoint — 2026-10-14 (backlog prompt Wave 0 + Track L: 5 issues closed, 8 commits pushed)

Ran `docs/NEXT-SESSION-issue-backlog.md` from a fresh session: Wave 0 (hygiene) plus Track L
steps 1, 2, 2b, 3 and 9 of `docs/NEXT-SESSION-free-llm-providers.md`. Nothing else was touched;
Waves 1-7 and Track L steps 4-8 are still ahead.

- **Issues closed with evidence comments:** #152, #164 (both already implemented, verified),
  #198 (regression test), #196 (install-from-clone + README), #33 (epic; decision: keep one
  repository and the `import lore` package). Open count 54 → **49**. #29 and #48 stay open with
  label `deferred` and a recommended decision each in `docs/TODO.md`.
- **Commits (all `review-gate` APPROVE, all on remote `main`, verified with `git ls-remote`):**
  `c6e4ff0` test(cli) exit code · `b6e6c74` docs(readme) install-from-clone · `4f7a474`
  docs(todo) deferred roadmap · `88f4726` fix(llm) jev hardening · `09821a1` feat(llm)
  `LORE_LLM_TIMEOUT` · `9a58385` feat(llm) free-only guard · `6e4802d` docs(llm) measurements ·
  `5dc276a` fix(tests) hermetic summary-card tests.
- **Full suite: 2676 passed, 25 skipped, 0 failed (442 s), ruff clean.** The long-standing failure
  `test_absent_summary_renders_at_a_glance_without_provider` is fixed: the summary job imports
  `OUTPUT_DIR` from `web_data` at call time while the tests patched only `web.OUTPUT_DIR`, so a job
  test wrote a summary row into the real output directory and every later run rendered "AI summary".
  Both tests now patch the directory the code reads; two consecutive runs of the file are green and
  the shared test home's `session_summaries` table stays at 0 rows.
- **Track L live check:** the OpenRouter key was fetched per command from 1Password (never stored,
  never printed). Both free models answered (9.8 s and 5.5 s), no 429, no cost. Limits read from
  `https://openrouter.ai/docs/api-reference/limits` on 2026-10-14: 20 req/min, 50 req/day below 10
  credits, 1000 req/day at ≥10 credits; this key reports `free_model_daily_requests {used 40,
  limit 1000}`. Recorded in `docs/LLM_INTEGRATION.md` ("Measured (2026-10-14)").
- **Test-home hygiene:** one stray summary row written by the old polluting test was removed from
  the *scratch* home `/tmp/opencode/lore-test-home/.lore/index_v2.sqlite` (backup:
  `/tmp/lore-test-home-index.sqlite.bak`). The live `~/.lore` archive was not touched.
- **Open finding, recommended decision in `docs/TODO.md`:** `review-gate` approves these commits
  while telling us the diff body was empty — file a `harness-idea` to embed the diff; meanwhile
  read an APPROVE for this repo as "no finding", not "line-level reviewed".

Next: Wave 1 (#199 → #193 → #92 → #176 → #112 → #185; #112 is sequential and data-loss-sensitive),
then Wave 2 before Track L steps 4-5.

## Current checkpoint — 2026-09-24

The 2026-09-23 Lore/Forgejo nightly made no source or tracker changes: all three
Claude windows failed before invocation because GNU `timeout` rejected `4h55m`;
MiniMax batch 1 hit provider error 2056 and batches 2–5 produced no report. A
preliminary OpenCode Lore audit was superseded and is not authoritative. The
nightly run worktree was removed. Local Lore `main` is clean and equal to
`github/main`; this docs-only closeout records the investigation and merge
state. GitHub has 51 open issues, zero open PRs, and only remote `main`. No code
tests were run for the failed nightly.

At the user's request, the separate cross-tool Skill Assistant work was merged:
`claude-setup#217`, `codex-setup#1`, `opencode#9`, `harness#18`, and `ai-stack#20`.
All five remote `feat/153-skill-assistant` branches were deleted; verification
found no matching remote refs. Forgejo reported the PRs mergeable with zero status
contexts and no branch protections. Prior focused results are recorded in the
older stack handoff (Codex 50, Claude pipeline 33 plus shell profile checks,
OpenCode 6, Harness 1, Antigravity 1); they were not rerun here.

On AI-Workstation, Lore's local `main` ref was at `b0e84a2` at last inspection,
while the canonical submodule checkout remains detached at `9ffb3ee`; the
`ai-stack` superproject records `d3953c1`. GitHub `main` has since advanced past
`b0e84a2`; the AI-Workstation checkout has not been updated to the docs-only
closeout. The superproject is 17 commits behind
`origin/main` and has unrelated dirty submodule pointers plus an untracked
`docs/night-ops/.../HALT.md`. Preserve those paths. Its other branches
(`autosync/*`, `feat/5-harness-rollout`, `fix/9-harness-ecc-remediation`,
`herdr-pilot`) are separate salvaged/ongoing stack work and were not merged or
deleted as part of this Lore/#153 task.

The failed run's final report is `ai-workstation:~/nightly-runs/issue-run-20260923/FINAL-REPORT.md`.
Raw issue snapshots and superseded local preliminary reports were cleaned up.
Before rerunning the issue work, fix the timeout duration format and compare
quota reset timestamps at stable precision; do not use a metered fallback.

> Repo: `~/projects/ai-stack/lore` · GitHub `DnaMes/lore` · default branch **main** · remote named **`github`** (not `origin`).
> `main` == `github/main` @ `2ac30bd`, working tree clean. All work is PUSHED.

## Goal

The maintainer asked (in-session) to work through the open GitHub issue backlog "in a loop":
triage-fix-verify each actionable issue, commit per batch with issue references, push, and
clean up. 32 issues were open at session start; **26 are now closed** (each with an
AI-disclaimer comment + commit reference on GitHub), **6 remain open on purpose** (below).

## Current Progress

- **13 commits on main, pushed** (`6b08635..2ac30bd`), spanning these batches:
  | Commit                | Batch                                                                       | Issues                   |
  | --------------------- | --------------------------------------------------------------------------- | ------------------------ |
  | `6b08635`             | A11y templates + snapshot regen                                             | #135 #136 #137           |
  | `6205544`             | Python perf: memoized index lookups + direct single-session reads           | #125 #126 #127 #128      |
  | `4f58d71`             | Tailwind precompiled CSS replaces ~400 KB JIT runtime; hljs defer           | #129                     |
  | `1b0342c`             | A11y base.html (skip-link, aria-labels, view-menu Space key)                | #130–#134                |
  | `d5740a5`             | Swallowed-exception cleanup (LLM/titles/digest/probe)                       | #114 #115 #121 #122 #123 |
  | `3f49c8b`             | CLI: `_load_existing_index_state` + `COMMANDS` dispatch table               | #117 #118                |
  | `c35d927`             | Hex-decode utility, ignored.json warning, decode tracing                    | #116 #120 #124           |
  | `0c69a90`             | `web_audit.py` extraction (partial #119)                                    | #119                     |
  | `0c5513d`             | Muted-text contrast AA/AAA both themes                                      | #146 #147                |
  | `04b5179` + `d88bee0` | opencode two-pass extraction (425 MB peak → bounded)                        | #104                     |
  | `52177c2`             | CSP-safe delegated event wiring, all inline `on*=` gone                     | #106                     |
  | `2ac30bd`             | .githooks path-pattern update (maintainer's own edit, committed on request) | —                        |
- **Test suite: 1164 passed, 10 skipped** (baseline at session start: 1118 passed). +46 tests.
- **ruff clean**, **mypy exactly at pre-session baseline** (24 pre-existing errors; the two-pass
  rewrite briefly added 12, fixed in `d88bee0`).
- GitHub state: 26 issues closed with comments; stale merged remote branches
  `hermes/issue-148-*` / `hermes/issue-101-*` deleted + pruned. No open PRs.

## What Worked

- **Test convention for this host:** the host's real `$HOME` has an unreadable
  `~/.local/state/network-restore-20260907/` dir that made 94 tests fail with PermissionError.
  Running with an **isolated HOME** gives a fully green suite:
  `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/ -p no:cacheprovider -q -o addopts=""`
  (baseline was 1118 passed / 10 skipped). Reuse this exact command.
- **Baseline-diffing instead of fixing:** mypy/lint noise was never "chased" — instead diffed
  against the pre-session baseline so only genuine regressions were addressed.
- **Patch-target preservation when moving code:** `web_jobs` resolves helpers via late
  `from .web import ...` and 30+ tests patch `web.load_index` / `web._audit_*`. Moving the
  audit cluster to `web_audit.py` kept every patch target alive by re-exporting with PEP 484
  redundant aliases (`from .web_audit import X as X`) and doing call-time late imports for
  `load_index`. The full suite confirmed it.
- **A `.venv` now exists in the repo** (438 MB, gitignored, editable install + `[dev]` extras,
  lore 2.4.0 on Python 3.12) — the project's ready-to-use test environment.

## What Didn't Work

- **Subagent delegation died mid-loop:** the Batch A subagent (implementer lane,
  `zai-coding-plan/glm-5.3`) hit a 5-hour usage quota AFTER completing #125/#126 but BEFORE
  #127/#128 — it returned a failure with work silently left in the working tree. Lesson:
  always `git status` + review the diff after a subagent failure before re-delegating; treat
  partial work as review input, not garbage. The rest of the loop ran in-session instead.
- **Stash-based mypy baseline diffing only works pre-commit:** after commits exist, `git stash`
  only holds leftover working-tree changes, so "baseline" and "current" mypy runs measure the
  same tree. For a true baseline after committing, diff against the pre-loop commit (e.g. via
  a worktree at `5cc8308`) or track the earlier measured numbers.
- **`git rm`'d files can't be re-`git add`ed by path** (fatal: pathspec did not match) — the
  deletion is already staged; just add the rest and commit.

## Decisions Made (each + why)

1. **#112 skipped entirely.** The maintainer's own comment re-scoped it: a real fix requires
   replacing `StreamingV2Writer`'s full-replace `begin()` (DELETE + cascade) with in-place
   upsert — a storage-layer redesign with regression risk for search/MCP memory/web (#35 was
   broken by exactly this before). Needs a dedicated scoped session. Issue stays OPEN.
2. **Roadmap issues #29/#33/#48/#92 skipped.** Feature/epic scope requiring design decisions,
   not loop-fixable. Stay OPEN.
3. **#119 done partially, stays OPEN** (comment posted on the issue). The framework-free
   audit cluster moved to `web_audit.py` (2246 → 2131 lines). Shrinking below the ~800-line
   target requires a Flask Blueprint refactor that invalidates most patch targets — a design
   decision, deliberately not forced in a sweep loop.
4. **#131 closed as "already implemented"** with evidence: `openSidebar`/`closeSidebar` in
   base.html already sync `aria-expanded` on every state change. Did not force a change.
5. **#144/#145 closed as informational** per their own "no urgent fix" text, with notes
   (legacy SearchEngine removal would degrade air-gapped installs — lazy-load instead).
6. **#106 payload context change:** continue-command moved from JS context (`|tojson` inside
   onclick) to an HTML attribute (autoescape). The escape-contract test was updated to pin the
   new rendering (`&#34;` for quotes) — same XSS safety, different context, test renamed.
7. **Tailwind theme colors kept byte-identical** in `tailwind.config.js` even though the four
   custom colors appear unused — exact-parity rebuild beats opportunistic cleanup here.
8. **`autosync/e14` branch left untouched** (local + remote): automated WIP snapshot from the
   maintainer's `repo-autosync.sh` infra with one unmerged commit — not session debris.
9. **Commit-then-push only on explicit user request** ("commite, push and merge") — the loop
   initially committed locally per batch and pushed at the end.

## Files Changed (by area; full detail in the commits above)

- `lore/services/index.py`, `lore/services/extraction.py`, `lore/services/__init__.py` —
  memoized finalized-index payload (stat + tombstone keyed), `session_by_id_map`,
  `find_live_session` service helper.
- `lore/extractors/base.py`, `lore/extractors/claude.py` — `find_session_by_id` hook +
  claude override (direct `<session_id>.jsonl` parse); decode tracing in `_decode_project_name`.
- `lore/extractors/opencode.py` — two-pass `extract_sessions` (light winner map → streamed
  parse), `_PendingSession`, shared `_SQLITE_SESSION_SELECT`; `_extract_sessions_from_sqlite`
  kept for direct callers.
- `lore/interfaces/web.py` (+ new `web_audit.py`) — O(1) session-meta lookup, audit/build-info
  cluster extracted with re-exports; CSP comment updated.
- `lore/interfaces/web_services.py` — `_group_threads_by_id` + memoized `thread_overview_by_id`.
- `lore/interfaces/mcp.py`, `lore/interfaces/mcp_tools/deps.py`, `lore/interfaces/mcp_tools/sessions.py` —
  shared memos for session/thread lookups, `find_live_session` wiring.
- `lore/storage/schema.py` (`open_v2_connection`), `lore/storage/tags.py`, `lore/storage/memory.py` —
  migration fast path.
- `lore/llm/gemini.py`, `lore/llm/ollama.py`, `lore/titles/generator.py`, `lore/digest.py`,
  `lore/interfaces/live_probe.py` — specific exceptions + debug logging.
- `lore_cli.py` — `_load_existing_index_state`, `COMMANDS` table, `build_parser()`.
- `lore/exporters/index.py` — ignored.json corruption warning.
- `lore/utils/text_processing.py`, `lore/extractors/cursor.py` — shared `decode_hex_or_str`.
- `lore/templates/*.html` (base, session, sessions, projects, session_rows, memory, dashboard,
  thread_detail, noise_rules) — a11y fixes, data-action delegation, data-confirm /
  data-auto-submit forms; `tailwind.config.js` + `tailwind.input.css` + `scripts/build_tailwind.sh`
  - `lore/interfaces/static/tailwind-compiled.min.css` (new), JIT runtime deleted.
- `scripts/vendor_assets.py` — tailwind JIT removed from the manifest.
- Tests: `tests/test_session_meta_lookup.py`, `tests/test_find_session_by_id.py`,
  `tests/test_decode_hex_or_str.py`, `tests/test_cli_dispatch.py` (new);
  `tests/test_vendored_assets.py`, `tests/test_storage_schema.py`, `tests/test_index_builder.py`,
  `tests/test_claude_extractor.py`, `tests/test_opencode_extractor_extended.py`,
  `tests/test_web_titles.py` extended; 10 template snapshots regenerated
  (`UPDATE_TEMPLATE_SNAPSHOTS=1`).

## Next Steps (ordered)

1. **#106 manual browser pass** (30 min): click through theme menu, sync menu, view toggles,
   resume modal, tag editor, delete confirms with devtools console open — the automated suite
   asserts zero CSP violations at page load, but click-flow verification was not done.
2. **#119 Blueprint refactor** (scoped session): split `web.py` routes into Flask Blueprints
   (api / pages / actions), move `_reload_sessions_index` into `web_jobs`, plan for
   `web.load_index`/`web._audit_*` patch-target migration across ~15 test files. Current state:
   audit cluster already extracted to `web_audit.py`; issue has a follow-up comment with the plan.
3. **#112 dedicated session** (the only `critical` left): replace `StreamingV2Writer`'s
   full-replace `begin()` with genuine upsert (UPSERT changed sessions, targeted DELETE for
   removed ones, leave unchanged message rows) so extractors can mtime-skip. Read the issue's
   maintainer comment FIRST — it documents why naive extractor-side skipping regressed #35.
4. **Roadmap (need maintainer decisions):** #92 hybrid-search Phase 2 distance cutoff, #48
   JSON-retirement exit criteria, #33 shared-memory vision, #29 MCP-over-HTTP.
5. If issues are fixed outside GitHub: remember each closed issue carries an AI-disclaimer
   comment; keep that convention.

## Gotchas (current, verified this session)

- Remote is named **`github`**; a pre-push hook warns `[pre-push] no origin remote` — harmless.
- Run the suite with **isolated HOME** (see What Worked); real `$HOME` breaks 94 tests via an
  unreadable network-restore dir.
- `pytest` needs `-o addopts=""` in this venv unless pytest-cov flags are wanted (pyproject
  addopts pull in `--cov`).
- Template snapshots: regenerate with `UPDATE_TEMPLATE_SNAPSHOTS=1` (documented in
  `tests/test_template_snapshots*`).
- `tests/test_vendored_assets.py::test_compiled_tailwind_covers_every_template_class_token`
  fails if a new template class is neither in the compiled Tailwind sheet nor a custom CSS
  class nor in `NON_STYLED_CLASS_TOKENS` — after adding new Tailwind classes, run
  `scripts/build_tailwind.sh` and commit the artifact.
- Pre-existing mypy/lint noise (24 mypy errors incl. `deps.py:62 index_path: object`,
  `writer.py:44`, `context.py`, `knowledge.py`, `tooling.py:59`) is known — do not chase.
- The `.githooks/pre-commit*` files are the maintainer's identity guard; leave them alone
  unless asked.
- repo-autosync gotcha from the 2026-07-03 handoff still applies: autosync leaves things
  staged; before committing, check `git diff --cached --name-only` and stage only intended files.

## Addendum 2026-09-23 — lore-sync memory/CPU (uncommitted)

`lore-sync.service` (host user unit, `export --all`) measured 1.5–4 min CPU and a ~3 GB RAM
peak per run at 1083 sessions. Timer was cut to 2 h by the p15 session (`OnCalendar=0/2:00:00`).

- **Root cause of the RAM peak:** one opencode session (3337 parts, 351 MB raw JSON) made
  `_load_parts_for_session_sqlite` peak at ~1.9 GB — `fetchall()` plus full `json.loads` of
  fields the transcript never uses (`state.metadata`, attachments). The #104 two-pass rewrite
  bounds *sessions*, not the parts of a single session. Fix: stream the cursor and reduce parts
  with `OpenCodeExtractor._slim_part`. Output digest over all 123 real sessions is identical.
- Also landed: `cmd_export` streams sessions into `IndexBuilder` (no list retention, tested but
  no measurable RAM win on its own); `StreamingV2Writer` queues only the first
  `_MAX_EMBED_CHARS` of each session for the embed pass (~160 MB); `_tag_git_info` caches git
  calls per project.
- **Measured (real data, `ru_maxrss`):** opencode extraction 2148 → 402 MB; full
  `export --all` 2888 → 1011 MB. Wall time still ~80 s: extraction re-parses every source each
  run.
- **Still open:** CPU. A true incremental export needs an extractor-level mtime skip *before*
  parsing, which is gated on replacing `StreamingV2Writer.begin()`'s full-replace with upsert
  (#112). Not started; needs its own scoped session.
- Tests: `tests/test_cli_export_streaming.py`, `tests/test_writer_embed_inputs.py`,
  `tests/test_opencode_part_memory.py`. Suite 1173 passed / 10 skipped.

---

generated by context-parachute v1.1.0
