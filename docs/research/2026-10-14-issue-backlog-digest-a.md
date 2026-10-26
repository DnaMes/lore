# Open-issue backlog digest, Part A: older issues and #191-#199

Snapshot at HEAD `136b0e0` (2026-10-14). Index and caveats: `docs/research/2026-10-14-issue-backlog-digest.md`.

## Part A


### #29 [#41] MCP-over-HTTP transport (streamable-http)
- STATUS: DEFERRED 2026-10-14 (label `deferred`; open, decision in `docs/TODO.md`: build after #174/#175 stabilise the search API). Originally OPEN (roadmap, maintainer-deferred 2026-05-18)
- SCOPE: Add `--transport streamable-http` (POST /mcp + SSE) to the MCP server and document it.
- ACCEPTANCE: [ ] `--transport streamable-http` flag exists; [ ] HTTP transport implemented; [ ] README + API_REFERENCE updated.
- FILES: `lore/interfaces/mcp.py`, `lore/cli/mcp.py`, `lore/interfaces/server.py`, `docs/API_REFERENCE.md`, `README.md`
- DEPENDS-ON: none
- SIZE: L; RISK: none
- FIX COMMAND: `lore-mcp --help 2>&1 | grep streamable-http` (currently absent; `docs/API_REFERENCE.md:5` lists only "MCP over stdio", `lore/cli/mcp.py` calls `server.run()` stdio-only).

### #33 Vision: tool-übergreifendes Agent-Gedächtnis (shared memory DB)
- STATUS: CLOSED 2026-10-14 (decision: keep one repository and the `import lore` package; all functional vision checkboxes shipped, 22 contract-tested extractors). Was PARTIAL — all functional vision checkboxes shipped in-repo; only the strategic repo/naming split and "widen extractor spectrum" remain.
- SCOPE: Decide the repo/naming split (or explicitly keep monorepo) and broaden extractor coverage; no code blocker.
- ACCEPTANCE: [x] v2 SQLite store (#44); [x] `memory_write`/`memory_recall` MCP + CLI; [x] semantic search (migration 10, `lore/storage/embeddings.py`); [x] memory web UI; [x] `memory_sources` provenance (`add_memory(source_session=...)`); [ ] repo-split decision.
- FILES: `lore/storage/memory.py`, `lore/storage/embeddings.py`, `lore/storage/schema.py`, `lore/interfaces/web.py`, `docs/superpowers/specs/`
- DEPENDS-ON: #44 (done)
- SIZE: L (decision+work); RISK: none
- FIX COMMAND: `git log --oneline --grep='#33' | head` + record maintainer decision in the issue (no test; it is a tracking epic).

### #48 P2: define and execute the JSON-retirement exit criteria (#44 follow-up)
- STATUS: DEFERRED 2026-10-14 (label `deferred`; open, decision in `docs/TODO.md`: start only after sub-task 4, the CI v2-vs-JSON equivalence test, exists). Was OPEN (maintainer-deferred to v3, "risky coexistence, not a blocker")
- SCOPE: Execute the 5 exit criteria then stop writing `index.json`/`index.sqlite` and make v2 the native shape.
- ACCEPTANCE: [ ] tombstones+ignored into v2; [ ] search routed through v2; [ ] `generated_at`/hash staleness assert; [ ] CI equivalence test v2 vs JSON on realistic data; [ ] `IndexBuilder` stops writing `index.json`/`index.sqlite`.
- FILES: `lore/services/index.py` (`load_index` v2-first then JSON fallback at 503-533), `lore/exporters/index.py:88-89`, `lore/storage/reader.py`, `lore/storage/schema.py`
- DEPENDS-ON: #34, #36, #44
- SIZE: L; RISK: format-compat (index schema/format)
- FIX COMMAND: `grep -n "index.json\|index.sqlite" lore/exporters/index.py` (still written; `~/.lore` still holds all three files).

### #92 Hybrid search: add a distance cutoff to semantic results (Phase 2)
- STATUS: CLOSED 2026-10-14 (commit `a781f75`). `_filter_semantic_rows` drops neighbours with `distance > semantic_max_distance()` before RRF fusion; the default 0.88 is the midpoint of related-p95 (0.777) / unrelated-p05 (0.986) measured on 28 labelled pairs with the real model (L2 over unit-normalised bge-small, norm 1.0000): keeps 14/14 related, drops 14/14 unrelated. `LORE_SEMANTIC_MAX_DISTANCE` overrides, `0` disables, unparsable falls back. Tool `tools/calibrate_semantic_cutoff.py`, note in `docs/archive/PLAN-hybrid-search.md`. Was OPEN
- SCOPE: Drop vec hits whose `distance` exceeds a calibrated threshold in `semantic_search_sessions`, optionally via `LORE_SEMANTIC_MAX_DISTANCE`.
- ACCEPTANCE: [ ] max-distance cutoff before RRF fusion; [ ] calibrated on the real bge-small distance distribution (percentile, not a constant); [ ] optional env override.
- FILES: `lore/storage/search.py` (`semantic_search_sessions` 247-316, `_knn_rows` 205-225), `lore/storage/fusion.py`, `docs/archive/PLAN-hybrid-search.md`
- DEPENDS-ON: #87 (done)
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts= -k "semantic or hybrid"` after adding a cutoff test (no `max_distance`/env exists today).

### #112 Incremental sync still fully parses unchanged session files before mtime check
- STATUS: CLOSED 2026-10-14 (commit `c0b1271`). `build_search_index` derives source fingerprints from the prior index entries (`_source_fingerprints`: path → mtime_ns, minimum parser_version per path, ambiguous mtimes force a re-parse) and hands them to every extractor, which skips the file before `open()` through the existing `BaseExtractor._source_unchanged`; `incremental=False` clears the maps. A skipped session keeps its prior JSON entry, its v2 message rows and its FTS row; a deleted source keeps its entry without a tombstone. Six tests in `tests/test_incremental_source_skip.py`. A/B on copies of the real archive (live `~/.lore` read-only): warm run 47 min vs full re-parse, identical id sets for the re-parsed tool (3193 claude-code sessions), 3192/3193 identical message counts — the single difference is a session whose id exists in two project copies that were both appended during the window (newest-copy rule, matching the live archive). Was PARTIAL
- SCOPE: Wire `known_source_mtimes`/`known_source_parser_versions` into `build_search_index` so its extractors skip unchanged files before `open()`.
- ACCEPTANCE: [x] `StreamingV2Writer.begin()` no longer full-replaces (now `BEGIN` + per-session upsert, `lore/storage/writer.py:544`); [x] extractors support pre-parse mtime skip (`lore/extractors/base.py:183`, `lore/extractors/claude.py:216`); [ ] the search-index/extraction path sets `known_source_mtimes`.
- FILES: `lore/services/extraction.py` (`build_search_index` 152-356; only post-parse check at 276-298), `lore/extractors/base.py`, `lore/extractors/claude.py`, `lore/storage/writer.py`, `lore_cli.py:429-430` (export path already sets it)
- DEPENDS-ON: #35, #96, #152
- SIZE: M; RISK: data-loss (a wrong skip empties v2 message rows — see #35 comment)
- FIX COMMAND: `grep -rn "known_source_mtimes" lore/ --include=*.py` (only `base.py` + `lore_cli.py`; expected to also appear in `lore/services/extraction.py`).

### #119 web.py: 2212-line/72-function module despite existing split-out siblings
- STATUS: PARTIAL — audit cluster extracted to `web_audit.py` (commit `0c69a90`), but the file has since grown.
- SCOPE: Split routes into Flask Blueprints (api/pages/actions) and move job/helper logic out to get `web.py` below ~800 lines.
- ACCEPTANCE: [x] audit/build-info cluster moved (`lore/interfaces/web_audit.py`); [ ] Blueprint split; [ ] `_reload_sessions_index` into `web_jobs`; [ ] `web.py` < ~800 lines.
- FILES: `lore/interfaces/web.py` (now 2637 lines, was 2212), `lore/interfaces/web_audit.py`, `lore/interfaces/web_jobs.py`, `lore/interfaces/web_services.py`, `lore/interfaces/web_helpers.py`
- DEPENDS-ON: none
- SIZE: L; RISK: none (but ~15 test files patch `web.*`, per issue comment)
- FIX COMMAND: `wc -l lore/interfaces/web.py` (target < 800).

### #191 Archive #41: Correct the Python distribution identity and installation instructions
- STATUS: OPEN
- SCOPE: Select/verify an available dist name at release time (proposed `lore-archive`), fix packaging metadata, README/Docker/CI/badges, keep `import lore`.
- ACCEPTANCE: [ ] available name verified at release; [ ] clean-venv install resolves to this project + all CLI entry points; [ ] `import lore` unchanged; [ ] docs/Docker/CI use corrected distinction; [ ] no PyPI-availability claim without fresh check.
- FILES: `pyproject.toml:6` (`name = "lore"`), `README.md`, `Dockerfile`, `.github/workflows/ci.yml`, `tests/test_cli_entry_points.py`
- DEPENDS-ON: #42, #44
- SIZE: M; RISK: migration (distribution rename breaks `pip install lore` users)
- FIX COMMAND: `python -m build && pip install dist/*.whl` in a clean venv, then check `lore --version`, `lore-web --help`, `lore-mcp --help`, `lore-web-probe --help` (web index availability needs a live PyPI check — NOT VERIFIED offline).

### #192 Archive #42: Ship a portable default Docker Compose deployment
- STATUS: OPEN
- SCOPE: Make `docker compose up` work on a clean host with no private DNS, external networks, Redis/Postgres, or workstation middleware.
- ACCEPTANCE: [ ] clean-host compose up without private DNS/external networks/middleware; [x] non-root user (`Dockerfile` `USER ai`); [ ] explicit least-privilege mounts documented; [ ] health/readiness + offline assets + semantic deps honest and tested.
- FILES: `docker-compose.yml` (hardcodes `dns: 192.168.50.1/192.168.1.1`, `networks: traefik_local: external: true`, Traefik labels), `Dockerfile`, `scripts/verify_offline_image.sh`
- DEPENDS-ON: #11, #30, #41, #44
- SIZE: M; RISK: none
- FIX COMMAND: `LORE_UID=$(id -u) LORE_GID=$(id -g) docker compose up -d && curl -fsS http://127.0.0.1:5000/api/health` on a host without `traefik_local` (currently fails on the external network).

### #193 Archive #43: Validate legitimate same-origin requests behind supported proxies
- STATUS: CLOSED 2026-10-14 (commit `d5918d3`). `LORE_PUBLIC_ORIGINS` (comma list of `scheme://host[:port]`) is parsed/normalised by `web_utils.parse_origin`/`normalize_origin` and honoured only with `TRUSTED_PROXY`; scheme and port are part of the comparison, malformed entries are dropped with a warning. While the flag is set, malformed `X-Forwarded-Proto/-Host/-For` on state-changing routes is `400` before the origin check; without it the headers are never parsed and the rate limiter's IP is untouched. `tests/test_proxy_origin.py` (25 tests: direct/trusted/untrusted/mixed), `docs/DEPLOYMENT.md` "Behind a reverse proxy". Was PARTIAL
- SCOPE: Trust forwarded headers only under explicit config; accept configured public scheme/host/port and reject unrelated/malformed/spoofed ones on state-changing routes.
- ACCEPTANCE: [x] `X-Forwarded-*` only when `TRUSTED_PROXY` set (`web.py:220`, `web_utils.py:78-87`); [ ] configured public scheme/host/port honored (incl. non-default ports/TLS); [ ] unrelated/malformed/spoofed rejected consistently; [ ] tests for direct/trusted/untrusted/mixed.
- FILES: `lore/interfaces/web.py:215-220,255-269`, `lore/interfaces/web_utils.py:76-90`, `tests/` (no proxy test found)
- DEPENDS-ON: #20, #42, #44
- SIZE: M; RISK: privacy (spoofed client IP / scheme escalation)
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts= -k "proxy or forwarded or origin"` (no such tests currently).

### #194 Archive #44: Require real browser interaction checks before release
- STATUS: OPEN — a standalone script exists (`tools/playwright_e2e_regression.py`) but it is not a repeatable pytest fixture and no release checklist records browser version/fixture/command/result.
- SCOPE: Add a repeatable browser fixture over synthetic data and wire the interaction/CSP/console checks into the release process.
- ACCEPTANCE: [ ] repeatable browser fixture populates sessions/messages/tags/search/pagination/code/nav; [ ] search/copy/tags/pagination/themes/resume/delete flows tested with keyboard + narrow viewport; [ ] no console errors/CSP violations/failed requests/dead controls; [ ] release checklist records browser version+fixture+command+result.
- FILES: `tools/playwright_e2e_regression.py`, `tests/` (no browser test), `HANDOFF.md` (#106 manual pass still open), `docs/audits/2026-09-12-lore-archive-audit.md:989`
- DEPENDS-ON: #20, #21, #23, #25, #26, #43
- SIZE: L; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts= -k browser` (no matches today).

### #196 docs: install from a clone instead of the ambiguous 'pip install lore' (child of #191)
- STATUS: CLOSED 2026-10-14 (commit `b6e6c74`; `tests/test_install_from_clone.py` builds a venv and asserts all five console scripts, `grep -n "pip install lore" README.md` empty, PyPI badges removed)
- SCOPE: Change README Quickstart to install from a clone and add a clean-venv install test for the five documented entry points.
- ACCEPTANCE: [ ] README no longer recommends `pip install lore`; [ ] clean-venv install-from-worktree test asserts `lore`, `lore-session`, `lore-web`, `lore-mcp`, `lore-web-probe`; [ ] `import lore` unchanged; [ ] no PyPI-availability claim; [ ] ruff + suite green.
- FILES: `README.md:26,107` (still `pip install lore`), `tests/test_cli_entry_points.py` (import-only, not build/install), `pyproject.toml:60-65`
- DEPENDS-ON: #191
- SIZE: S; RISK: none
- FIX COMMAND: `grep -n "pip install lore" README.md` (must return nothing) + new `tests/test_install_from_clone.py`.

### #197 Cognee-inspired memory lifecycle: improve pipeline, feedback, review queue, context block
- STATUS: PARTIAL — phases 1, 2 and 3.2 are implemented; phase 3.3 review queue and all of phase 4 are missing.
- SCOPE: Finish `lore memory review accept|reject` (+ Candidates web tab) and `lore context` with the budgeted memory-only block.
- ACCEPTANCE (design §Acceptance): [x] Phase 1: fake-provider test proves message text reaches the prompt and unchanged session skipped (`tests/test_knowledge_extraction.py::test_prompt_contains_message_text`); [x] Phase 2: `memory_feedback` (migration 15) + `similar`/`supersedes`; [x] Phase 3.2 `lore improve` stages; [ ] Phase 3.3 `lore memory review accept|reject` + `/memory` Candidates tab; [ ] Phase 4 `lore context [--project P] [--tool T] [--budget 1200]`.
- FILES: `lore/storage/schema.py` (migrations 14-18; `session_knowledge`, `memory_feedback`, review status), `lore/services/knowledge.py`, `lore/services/improve.py`, `lore/storage/memory.py:848` (`set_memory_status`), `lore_cli.py` (`COMMANDS`: has `improve`, no `context`/`memory review`), `docs/superpowers/specs/2026-10-13-cognee-inspired-memory-design.md`
- DEPENDS-ON: #187, #92
- SIZE: L; RISK: privacy (LLM stage sends transcript text; cloud providers already require explicit opt-in)
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts= -k "improve or knowledge or feedback or context"` plus `python lore_cli.py context --help` (unknown command today).

### #198 CLI: main() discards handler return values, every command exits 0
- STATUS: CLOSED 2026-10-14 (regression test added in commit `c6e4ff0`; fix itself landed in `39b5ae8`). Was PARTIAL — the reported bug is NO LONGER TRUE; exit propagation now works, but the acceptance test and the exact `return handler(...)` form are still missing.
- VERDICT on claim: false at HEAD. `lore_cli.py:2169-2171` is `rc = handler(args)` / `if isinstance(rc, int) and rc != 0: sys.exit(rc)`. Landed incidentally in commit `39b5ae84` ("feat(export): add opt-in redacted exports for sharing (#197)"), which changed `handler(args)` → `rc = handler(args)` + `sys.exit(rc)`.
- OBSERVED PROOF: stub `COMMANDS['list'] = lambda a: 1; main()` → printed `MAIN_EXIT_CODE 1`.
- SCOPE: Add a regression test that runs `main()` through a stub handler returning 1; optionally switch to `return rc` per the issue text.
- ACCEPTANCE: [x] non-zero int handler result becomes the process exit code; [ ] dedicated test; [ ] audit of commands whose exit code changed (no systemd/hook dependency found in-repo).
- FILES: `lore_cli.py:2161-2172`, `pyproject.toml:61`, `tests/test_cli_dispatch.py`
- DEPENDS-ON: none (related #197)
- SIZE: S; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_cli_dispatch.py -p no:cacheprovider -q -o addopts=` after adding an `exit_code` test.

### #199 Follow-ups from the phase 1 review gate (#197)
- STATUS: CLOSED 2026-10-14 (commits `4dd4fa0`, `540ab30`, `aea6b15`, `ceb11e6`, `c426357`). Scoped role check runs the FTS engine (throwaway `:memory:` FTS5 with `SCOPE_FTS_TOKENIZER`) instead of `LIKE %term%`; `_has_stored_vectors` takes the query's filters; the Python vector scan is bounded (`LORE_MEMORY_VECTOR_SCAN_LIMIT`, default 20000, filters pushed into SQL); `ExtractionReport` splits provider-unavailable/provider-error/unparsable and exit 1 prints the breakdown; `save_session_knowledge` commits explicitly; validator tests pin tab/newline edges and allow apostrophes in queries; the recall floor is calibrated to **0.6** on 61 labelled pairs (`tools/calibrate_recall_floor.py`, `docs/research/2026-10-14-recall-floor-calibration.md`). Proof: `pytest -k "memory or snippet or fts_query or scope"` → 328 passed. Was PARTIAL
- SCOPE: Work the nine independent bullets, starting with the scoped-search role check (returns wrong hits).
- ACCEPTANCE: [x] batch-load tags/sources/feedback in `_load_entries` (`memory.py:434-486`), incl. the `limit*4` over-fetch; [x] `snippet(...)` uses one shared constant (`SEARCH_INDEX_BODY_COLUMN`, `memory.py:661`, `search.py:113`); [ ] scoped-search role check uses FTS token/phrase rules, not `LIKE %term%` (`search.py:158-190`); [ ] `_has_stored_vectors` restricted to the query's kind/project filters (`memory.py:626`); [ ] cap/vec0 table for the full Python memory-vector scan (`memory.py:542-597` — `min_similarity` added, scan still unbounded); [ ] split `ExtractionReport.failed` into provider-unavailable vs unparsable (`knowledge.py:108,144`; `llm/tasks/knowledge.py`); [~] edge-case tests: `a;b`, `"foo\n"`, `"\r..."`, `"foo\nbar"` covered (`tests/test_fts_query.py:63-90`), leading tab and `don't` not explicitly covered; [ ] explicit commit in `save_session_knowledge` (relies on autocommit `open_v2_connection`, `schema.py:357`); [~] 0.7 floor still a documented PLACEHOLDER (`memory.py:69-74`).
- FILES: `lore/storage/memory.py`, `lore/storage/search.py`, `lore/storage/knowledge.py`, `lore/storage/schema.py`, `lore/services/knowledge.py`, `tests/test_fts_query.py`
- DEPENDS-ON: #197, #92
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts= -k "memory or snippet or fts_query or scope"` after adding the per-bullet tests.

## Summary table

| # | STATUS | SIZE | RISK | one-line scope |
|---|--------|------|------|----------------|
| 29 | DEFERRED | L | none | Add `--transport streamable-http` MCP transport + docs |
| 33 | CLOSED 2026-10-14 | L | none | Decide repo/naming split; broaden extractor spectrum |
| 48 | DEFERRED | L | format-compat | Execute JSON-retirement exit criteria, stop writing JSON/sqlite |
| 92 | CLOSED 2026-10-14 | M | none | Calibrated max-distance cutoff in `semantic_search_sessions` |
| 112 | CLOSED 2026-10-14 | M | data-loss | Wire `known_source_mtimes` into `build_search_index` for pre-parse skip |
| 119 | PARTIAL | L | none | Blueprint-split `web.py` below ~800 lines |
| 191 | OPEN | M | migration | Correct distribution name + install docs (`lore-archive`) |
| 192 | OPEN | M | none | Portable compose without private DNS/external networks |
| 193 | CLOSED 2026-10-14 | M | privacy | Configured-proxy same-origin validation + test matrix |
| 194 | OPEN | L | none | Repeatable browser interaction fixture + release checklist |
| 196 | CLOSED 2026-10-14 | S | none | README install-from-clone + clean-venv entry-point test |
| 197 | PARTIAL | L | privacy | Finish review queue (3.3) and `lore context` (phase 4) |
| 198 | CLOSED 2026-10-14 | S | none | Bug false at HEAD; add exit-code regression test |
| 199 | CLOSED 2026-10-14 | M | none | Fix scoped role check, vector scan cap, explicit commit, etc. |

## NOT VERIFIED
- PyPI availability of `lore` / `lore-archive` (no web access; requires a fresh index check at release).
- Whether an external `lore-sync` systemd unit or hook relies on the old always-0 exit code: no such unit/hook is in this repo (could not inspect host units; `#198`).
- Clean-host Docker behavior: not executed (no clean host / external `traefik_local` network).
- Actual browser interaction behavior: `tools/playwright_e2e_regression.py` was read but not executed.
