# Open-issue backlog digest, Part C: Archive #21-#40 (#171-#190)

Snapshot at HEAD `136b0e0` (2026-10-14). Index and caveats: `docs/research/2026-10-14-issue-backlog-digest.md`.

## Part C


## Clusters (recommended ship order)

1. **C1 – Archive query correctness (#171, #172, #173, #174, #175).** Reason: shared data path — date/tag filters (#172/#173) must exist before #174 can constrain candidates pre-limit, and #175's message evidence needs #171's stable message identity/permalink. Smallest blast radius, mostly storage/web/API code plus tests.
2. **C2 – Web code-block clipboard (#176).** Standalone small frontend fix; ships anytime, cheap win.
3. **C3 – Analysis pipeline contracts (#177, #178, #179, #186).** Reason: hydration (#177) defines canonical task input; coverage (#178) and resumable jobs (#179) build on it; #186 cache keys depend on the request identity (#180) and revision fields introduced here. Overlaps heavily with already-merged `lore summarize` (#197): `lore/services/summarize_chunks.py`, `lore/services/summarize.py`, `lore/storage/summaries.py`, `session_summaries` table already implement chunking, fingerprints and per-chunk resume — extend, do not rebuild.
4. **C4 – Provider boundaries and native adapters (#180, #181, #182, #183, #184, #185).** Reason: #180 defines the adapter contract the four CLI adapters (#181–#184) implement and title routing (#185) consumes. **POLICY FLAGS:** #180–#184 all overlap with the merged `openrouter`/`jev` providers — do not duplicate lanes; native CLI adapters must use native auth only, never copy CLI tokens into API requests or rotate/fan out Google/`agy` accounts (#183 especially). Transcript-bearing tasks must remain on the jev local/fastcheap allowlist (`lore/llm/jev.py:18`) and MCP summarize's local-only gate (`lore/services/summarize_cli.py:131-145`); no adapter may send transcripts to agentic delegate lanes.
5. **C5 – Knowledge/memory/workflow/retrieval (#187, #188, #189, #190).** Reason: needs C3's durable jobs/cache keys (#29/#36 deps) and is where the #197 memory lifecycle (`lore improve`, `lore memory review`, cognee spec) already landed — #187/#189 must integrate with, not duplicate, `lore/storage/memory.py` statuses and `lore/services/improve.py`.

### #171 Archive #21: durable message permalinks and bounded transcript loading
- STATUS: PARTIAL. In-page anchors `#msg-N` exist (lore/templates/session.html:97,121,339) and a bounded page fragment route exists (lore/interfaces/web.py:1560 `/session/<id>/messages?page=N`), but that route calls `_enriched_session_for_detail` which materializes the whole session before slicing `pairs[offset:offset+N]` (web.py:1575-1584). No deep-link API returning a bounded segment, no permalink contract surviving reindex/restore, deleted-message handling only via generic 404.
- SCOPE: Add a durable message-identity route/API that loads only the requested message window from the v2 `messages` table and explicit not-found/gone responses.
- ACCEPTANCE: [ ] stable permalink survives reindex/restore [ ] bounded page loads only requested segment [ ] deep link returns exact message + context [ ] missing/deleted ids return explicit responses
- FILES: lore/interfaces/web.py, lore/storage/reader.py, lore/web/session_view_prep.py, tests/test_session_pagination.py (new: tests/test_message_permalink.py)
- DEPENDS-ON: #154 (#04), #160 (#10), #168, #170; blocks #175
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_message_permalink.py -p no:cacheprovider -q -o addopts=` (test to be created)

### #172 Archive #22: date filtering across time zones and inclusive end dates
- STATUS: OPEN. lore/utils/datetime.py is 76 lines with no ZoneInfo/DST handling; `parse_timestamp` uses local `fromtimestamp` (datetime.py:15) and `make_naive` strips tzinfo (datetime.py:27-29); CLI compares naive datetimes (lore_session_cli.py:25); MCP mixes `datetime.fromisoformat` aware/naive (lore/interfaces/mcp_tools/search.py:142). Only 8-line test file (tests/test_datetime_utils.py). No date param at all in `search_sessions`.
- SCOPE: Centralize timezone-aware date-range parsing with inclusive end-date handling and reuse it in CLI/web/API/MCP filters.
- ACCEPTANCE: [ ] timezone-explicit normalization [ ] inclusive end date [ ] DST/naive-legacy/mixed-format tested [ ] all four surfaces agree
- FILES: lore/utils/datetime.py, lore/interfaces/mcp_tools/search.py, lore/interfaces/web.py, tests/test_datetime_utils.py (extend)
- DEPENDS-ON: #162, #174 → actually #162/#174 map to #162(#12),#174(#24),#175(#25); feeds #174
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_datetime_utils.py -p no:cacheprovider -q -o addopts=` (table-driven cases to be added)

### #173 Archive #23: connect saved tags and bookmarks to archive filtering
- STATUS: PARTIAL. Tags persist in v2 `session_tags` and survive rebuilds (lore/storage/tags.py, tests/test_session_tags.py); web list filtering by tag exists (lore/interfaces/web.py:973-1011, routes /api/tags at :1903, tag param in /sessions at :1061); MCP sessions tool surfaces tags (lore/interfaces/mcp_tools/sessions.py:60). Missing: `search_sessions` has no tag filter (lore/storage/search.py:45-150), CLI list has no tag filter, counts/empty states untested outside web, unknown/deleted-session tag handling untested.
- SCOPE: Thread tag/bookmark constraints through search, CLI listing and MCP with counts and durable-mutation tests.
- ACCEPTANCE: [ ] filters in CLI/API/web/MCP [ ] counts+pagination+empty states respect tags [ ] durable across rebuild/restore [ ] unknown/deleted handled without losing annotation
- FILES: lore/storage/search.py, lore/storage/tags.py, lore_session_cli.py, lore/interfaces/mcp_tools/sessions.py, tests/test_session_tags.py
- DEPENDS-ON: #152 (#02), #161 (#11), #172, #175; feeds #174
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_session_tags.py tests/test_storage_search.py -p no:cacheprovider -q -o addopts=`

### #174 Archive #24: apply search filters before limiting candidates
- STATUS: OPEN (heuristic mitigation only). `search_sessions` fetches `LIMIT max(1,limit)*4` unfiltered rows then post-filters tool/project/scope in Python (lore/storage/search.py:113-147); `_filter_semantic_rows` does the same after the KNN cut-off (search.py:225-244, comment at :288 admits filters run after cut-off). No date/tag filters in either path; CLI/web/MCP callers pass tool/project only.
- SCOPE: Push tool/project/date/tag/role filters into the SQL candidate selection (and KNN pre-filter) before ranking/limit on every search entry point.
- ACCEPTANCE: [ ] filters constrain before ranking/limit [ ] top-N-unfiltered-elsewhere fixture returns best filtered matches [ ] FTS/semantic/hybrid + all surfaces consistent [ ] invalid filters → validation errors
- FILES: lore/storage/search.py, lore/storage/fusion.py, lore/interfaces/mcp_tools/search.py, tests/test_storage_search.py, tests/test_hybrid_search.py
- DEPENDS-ON: #172, #173, #175, #190
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_storage_search.py tests/test_hybrid_search.py -p no:cacheprovider -q -o addopts=` (filter-before-limit fixture to be added)

### #175 Archive #25: searchable message evidence and paginated results
- STATUS: PARTIAL. Hits carry sentinel-delimited snippet + source (lore/interfaces/api_payloads.py:42-53; storage/search.py snippet at :111). Missing: no message identity/permalink in hits, `/api/search` is fixed `limit=10` with no cursor (lore/interfaces/web.py:2214-2216), MCP search default limit 10, no pagination, redaction labels not surfaced.
- SCOPE: Add message-level evidence (seq, excerpt, permalink) and cursor pagination to search API/CLI/MCP.
- ACCEPTANCE: [ ] per-hit message evidence + stable id + permalink [ ] cursor reaches every match beyond first ten [ ] filters before page boundary, explicit totals [ ] redaction/truncation labelled
- FILES: lore/storage/search.py, lore/interfaces/api_payloads.py, lore/interfaces/web.py, lore/interfaces/mcp_tools/search.py, tests/test_api_contract.py
- DEPENDS-ON: #154, #160, #171, #174, #190
- SIZE: L; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_api_contract.py tests/test_mcp_scoped_search.py -p no:cacheprovider -q -o addopts=`

### #176 Archive #26: unify code-block enhancement, exact clipboard content
- STATUS: PARTIAL. One idempotent enhancement path exists (lore/templates/base.html:1573 guard `el.closest('.code-card')`, copy button at :1630-1641 using `navigator.clipboard.writeText`), CSP-safe (no inline handlers). Missing: enhancement IIFE runs once at load; lazy-loaded appended pairs (lore/interfaces/web.py:1560 fragment route + session.html loader) are never re-enhanced — no MutationObserver or exported enhance function; payload `el.innerText||el.textContent` (base.html:1633) may not preserve exact whitespace; no browser tests in repo (no playwright/selenium dependency).
- SCOPE: Export a single idempotent enhancer, invoke it after appended content, and copy raw code text exactly.
- ACCEPTANCE: [ ] one idempotent path for initial+appended [ ] clipboard = exact code text [ ] keyboard + feedback accessible [ ] CSP clean
- FILES: lore/templates/base.html, lore/templates/session.html, tests/test_render_smoke.py, tests/test_session_detail_markup.py
- DEPENDS-ON: #170, #171, #194 (#44)
- SIZE: S; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_render_smoke.py tests/test_session_detail_markup.py -p no:cacheprovider -q -o addopts=` (browser-level clipboard assertions need a browser harness — NOT VERIFIED here)

### #177 Archive #27: hydrate archived transcripts before AI analysis
- STATUS: PARTIAL. `lore summarize` feeds the full archived message list through `summary_input.compress` (lore/services/summary_input.py, lore/services/summarize.py) with seq coverage and fingerprint — good. But KnowledgeExtractor truncates to `session.messages[:20]` (lore/llm/tasks/knowledge.py:69-77) and titles use `max_messages=6` (lore/titles/generator.py:277,285) — lossy excerpts; no source-revision/coverage metadata in task inputs; explicit-fail-on-unavailable exists in summarize but not in knowledge/title paths.
- SCOPE: Give every transcript-bearing task (knowledge, titles, stats) the hydrated canonical messages plus revision/coverage metadata, and explicit failure when hydration is impossible.
- ACCEPTANCE: [ ] canonical archived messages, not excerpts [ ] revision+coverage in task metadata [ ] explicit failed/partial state [ ] formatting/export work with AI disabled
- FILES: lore/llm/tasks/knowledge.py, lore/titles/generator.py, lore/services/summary_input.py, lore/services/knowledge.py, tests/test_summary_input.py, tests/test_knowledge_extraction.py
- DEPENDS-ON: #154, #160, #165, #178; feeds #178–#186
- SIZE: M; RISK: privacy (more transcript text sent to providers — keep jev local/fastcheap allowlist and redaction)
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_summary_input.py tests/test_knowledge_extraction.py tests/test_llm_provider_privacy.py -p no:cacheprovider -q -o addopts=`

### #178 Archive #28: complete sessions with explicit analysis coverage
- STATUS: PARTIAL. Chunking with ordered chunks, late-chunk supersession and fingerprints exists (lore/services/summarize_chunks.py:59-77 sample_chunks, _MERGE_INSTRUCTIONS at :20; summarize.py:747 `_process_chunked`). BUT `sample_chunks` deliberately drops middle chunks when >8 (truncation flag) — coverage is not complete; digest/knowledge tasks still excerpt-based; no "covered revision/message range/count" metadata payload; partial-vs-complete distinction only via `truncated` bool.
- SCOPE: Either cover all messages (raise/iterate chunk budget) or report explicit coverage metadata and never mark truncated runs complete; extend to knowledge/digest tasks.
- ACCEPTANCE: [ ] bounded ordered chunks covering all messages [ ] synthesis includes late corrections + coverage metadata [ ] limits honored and visible [ ] partial never reported complete
- FILES: lore/services/summarize_chunks.py, lore/services/summarize.py, lore/digest.py, lore/llm/tasks/knowledge.py, tests/test_summarize_chunking.py
- DEPENDS-ON: #154, #177, #179, #186
- SIZE: M; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_summarize_chunking.py tests/test_summarize_pipeline.py -p no:cacheprovider -q -o addopts=`

### #179 Archive #29: persist resumable analysis jobs, preserve old results
- STATUS: PARTIAL. Per-chunk results persist in `session_summaries` keyed by scope+fingerprint and are reused on re-run (summarize.py:786-798 `get_summary`/fingerprint check; lore/storage/summaries.py:281 `has_current_summary`). Missing: web summary jobs are in-memory dicts (lore/interfaces/web_jobs.py:25 `RELOAD_JOBS`), lost on restart; no durable job row with cancellation/timeout/provider-failure/retry states; `save_summary` UPSERT overwrites the same `(level, scope_key)` (summaries.py:144-160) so previous artifacts are not preserved.
- SCOPE: Add a durable job table (request identity, revision, chunk progress, provider/model, distinct states) and non-overwriting artifact retention.
- ACCEPTANCE: [ ] job persists identity/revision/progress/provider/model/errors/state [ ] resume unfinished chunks idempotently [ ] new runs preserve old artifacts [ ] cancel/timeout/failure/retry distinguishable
- FILES: lore/storage/schema.py, lore/storage/summaries.py, lore/interfaces/web_jobs.py, lore/services/summarize.py, tests/test_summaries_storage.py
- DEPENDS-ON: #161 (#11), #177, #178, #180, #186
- SIZE: L; RISK: migration (new table), data-loss if upsert retained
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_summaries_storage.py tests/test_summarize_pipeline.py -p no:cacheprovider -q -o addopts=`

### #180 Archive #30: explicit native-CLI and local-model provider boundaries
- STATUS: PARTIAL. `get_provider` raises on unknown provider with no implicit paid fallback (lore/llm/factory.py:50-57); jev lanes restricted to local/fastcheap (lore/llm/jev.py:18-44); MCP summarize local-only gate (lore/services/summarize_cli.py:131-145); `LLMConfig.for_transcripts` disables disk caching (lore/llm/base.py:40-48). Missing: no persisted request fields (auth mode, timeout, cancellation, execution policy), no native CLI adapters, gemini/ollama/openrouter still mix selection+invocation; no adapter interface.
- SCOPE: Define an adapter interface with persisted explicit request fields and implement native-CLI + local endpoints behind it; **policy: never route transcripts to agentic delegate lanes and never fan out/rotate agy/Google accounts.**
- ACCEPTANCE: [ ] provider/model/auth-mode/timeout/cancellation/policy persisted per request [ ] native CLI adapters use native auth, no token copying [ ] Ollama stays opt-in local [ ] missing binary/auth/timeout/nonzero-exit explicit
- FILES: lore/llm/factory.py, lore/llm/base.py, lore/llm/jev.py, lore/llm/gemini.py, lore/llm/ollama.py (new: lore/llm/adapters/)
- DEPENDS-ON: #179, #181-#185, #186
- SIZE: L; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_jev_provider.py tests/test_llm_provider_privacy.py tests/test_cli_openrouter_optin.py -p no:cacheprovider -q -o addopts=`

### #181 Archive #31: native Codex CLI analysis adapter
- STATUS: OPEN. No Codex adapter in lore/llm (factory supports gemini/jev/ollama/openrouter only); Codex exists only as extractor (lore/extractors/, tests/test_codex_extractor.py). Overlap flag: adapter must not duplicate jev delegate lanes; native codex auth only, no transcript to agentic lanes.
- SCOPE: Implement Codex CLI adapter boundary (detect/version/structured output/timeout/cancel) against fake executables.
- ACCEPTANCE: [ ] detect CLI/version/capabilities [ ] native auth + structured output + bounded input [ ] parse success/malformed/partial/failed into job contract [ ] fake executables, no real credentials
- FILES: lore/llm/ (new adapter module), lore/llm/factory.py, lore/llm/base.py, tests/ (new test file)
- DEPENDS-ON: #177, #179, #180, #186
- SIZE: M; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_codex_adapter.py -p no:cacheprovider -q -o addopts=` (new)

### #182 Archive #32: native Claude Code analysis adapter
- STATUS: OPEN. No Claude analysis adapter; Claude exists as extractor only. Same boundary and policy constraints as #181.
- SCOPE: Implement Claude Code CLI adapter with native auth, model/timeout/cancel enforcement, fake-executable tests.
- ACCEPTANCE: [ ] detect availability/version [ ] native auth, no credential copying [ ] enforce model/timeout/cancel/coverage metadata [ ] fake executables cover all failure states
- FILES: lore/llm/ (new adapter), lore/llm/factory.py, tests/ (new)
- DEPENDS-ON: #177, #179, #180, #186
- SIZE: M; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_claude_adapter.py -p no:cacheprovider -q -o addopts=` (new)

### #183 Archive #33: native Antigravity CLI analysis adapter
- STATUS: OPEN / NEEDS-DESIGN (body itself requires confirming the supported CLI contract before coding). No adapter; Antigravity exists only as extractor (lore/extractors/antigravity.py, tests/test_antigravity_extractor.py, test_extractor_antigravity_transcripts.py). **POLICY FLAG: must not fan out or rotate Google/`agy` accounts; adapter defaults to unavailable and never uses subscription credits implicitly.**
- SCOPE: Confirm the Antigravity CLI contract first, then implement a restricted, cancellable adapter that reports unavailable by default.
- ACCEPTANCE: [ ] availability false without supported CLI/capability [ ] restricted bounded native-auth invocation [ ] distinct persisted states [ ] fake-executable policy tests
- FILES: lore/llm/ (new adapter), lore/llm/factory.py, docs/ (contract note), tests/ (new)
- DEPENDS-ON: #158 (#08), #179, #180, #186
- SIZE: L; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_antigravity_adapter.py -p no:cacheprovider -q -o addopts=` (new)

### #184 Archive #34: native Gemini CLI analysis adapter
- STATUS: OPEN. lore/llm/gemini.py is a Google API client only; no CLI adapter and no explicit CLI-vs-API separation. **POLICY FLAG: CLI credentials must never be copied into Google API config; no agy account rotation.**
- SCOPE: Split Gemini API and Gemini CLI adapters; CLI adapter uses native auth and documented structured output, explicit failure when CLI missing.
- ACCEPTANCE: [ ] missing CLI reported without silent fallback [ ] native auth + documented structured output [ ] no credential copying across boundary [ ] fake binaries cover all failure states
- FILES: lore/llm/gemini.py, lore/llm/factory.py, tests/test_openrouter_provider.py (pattern), tests/ (new)
- DEPENDS-ON: #177, #179, #180, #186
- SIZE: M; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_gemini_adapter.py tests/test_llm_provider_privacy.py -p no:cacheprovider -q -o addopts=` (new)

### #185 Archive #35: route generated titles through explicitly selected provider
- STATUS: OPEN (PARTIAL behavior). lore/titles/generator.py has an implicit strategy chain with silent fallthrough (`_generate_auto` at :119 tries llm → keywords → first_message; per-provider paths :164/:193); selection is not recorded, excerpts untracked, no explicit-fallback setting. MCP summarize already restricts to local providers (summarize_cli.py) — titles have no equivalent gate.
- SCOPE: Record and honor an explicitly selected title provider/model; remove implicit cross-provider fallback; record excerpt/revision/prompt-version/status.
- ACCEPTANCE: [ ] request records+honors selected provider/model [ ] no implicit fallback sends content elsewhere [ ] excerpt/revision/prompt-version/status recorded [ ] fast/local title behavior still compatible
- FILES: lore/titles/generator.py, lore/services/extraction.py, lore/services/summarize_cli.py, tests/test_web_titles.py
- DEPENDS-ON: #177, #180, #186
- SIZE: M; RISK: privacy
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_web_titles.py tests/test_import_quality_and_titles.py -p no:cacheprovider -q -o addopts=`

### #186 Archive #36: version AI caches by source revision and request identity
- STATUS: PARTIAL. `summary_fingerprint` covers compressed text + model + prompt_version + compressor_version (lore/storage/summaries.py:22-36); chunk scope keys include seq range (summarize_chunks.py:31); unique `(level, scope_key)` upsert prevents cross-association (summaries.py:144-160). Missing: provider identity and parameters not in fingerprint (model label only), no canonical source-revision component, old results overwritten not preserved, title cache is an unversioned JSON dict (generator.py:285-306), jev disk cache keyed by prompt hash only (jev.py:158).
- SCOPE: Add source revision + effective provider/params to all cache keys (SQLite unique constraints), preserve superseded rows.
- ACCEPTANCE: [ ] keys include revision/provider/model/params/prompt version/task/coverage [ ] targeted invalidation [ ] old results queryable with metadata [ ] concurrent workers safe
- FILES: lore/storage/summaries.py, lore/services/summarize.py, lore/titles/generator.py, lore/llm/jev.py, tests/test_summaries_storage.py
- DEPENDS-ON: #151 (#01), #160, #178, #179, #180, #185
- SIZE: M; RISK: migration (key format change), format-compat
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_summaries_storage.py tests/test_summarize_pipeline.py -p no:cacheprovider -q -o addopts=`

### #187 Archive #37: promote generated knowledge into memory with reviewable evidence
- STATUS: PARTIAL. Strong overlap with merged #197 memory lifecycle. Exists: statuses candidate/confirmed/rejected (lore/storage/memory.py:64-66), append-and-supersede with `superseded_by` (memory.py:10-11, :184-205), `memory_sources` with message ids (memory.py:284, :891-900), review via `lore memory review`, `lore improve` stores distilled knowledge as `status="candidate"` (lore/services/improve.py:206-214) and does not auto-activate rules. Missing: observation-vs-hypothesis distinction, confidence field, model/request metadata on candidates, audit-complete export.
- SCOPE: Extend memory candidates with confidence + generation request metadata and observation/hypothesis typing, keeping #197 flows intact.
- ACCEPTANCE: [ ] candidates carry spans/revision/request metadata/confidence [ ] distinct states incl. superseded [ ] review transitions preserve artifacts/evidence [ ] export includes audit evidence
- FILES: lore/storage/memory.py, lore/storage/schema.py, lore/services/improve.py, lore/services/knowledge.py, tests/test_storage_memory.py, tests/test_improve.py
- DEPENDS-ON: #160, #179, #186, #188, #189
- SIZE: M; RISK: migration
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_storage_memory.py tests/test_memory_supersede_atomic.py tests/test_improve.py tests/test_mcp_memory.py -p no:cacheprovider -q -o addopts=`

### #188 Archive #38: evidence-backed workflow analysis
- STATUS: NEEDS-DESIGN. No metric vocabulary exists: lore/llm/tasks/stats.py produces LLM "patterns/productivity/recommendations" (inferred, undefined), lore/digest.py builds counts only, schema has bare `git_branch`/`git_commit` (lore/storage/schema.py:57-58) with no correlation model. Nothing shipped.
- SCOPE: Design and implement a small metric vocabulary with documented definitions, separating observed events / computed metrics / labelled inferences, linked to message evidence.
- ACCEPTANCE: [ ] every metric has definition+source fields+coverage limits [ ] three distinct result types [ ] ambiguity reported, no invented causation [ ] reproducible, evidence-linked
- FILES: new lore/services/workflow.py (or lore/metrics/), lore/storage/schema.py, lore/digest.py, lore/llm/tasks/stats.py, tests/ (new)
- DEPENDS-ON: #160, #162, #187, #190
- SIZE: L; RISK: none
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_workflow_analysis.py -p no:cacheprovider -q -o addopts=` (new)

### #189 Archive #39: export reviewed harness recommendations with evidence
- STATUS: OPEN. lore/utils/rules.py is 43 lines of keyword sentence extraction with no evidence, status, or export (rules.py:7-43); web /rules route exists (lore/interfaces/web.py:2057) and noise-rules CRUD exists, but no project-scoped, evidence-backed recommendation lifecycle, no activation workflow, no portable export.
- SCOPE: Build project-scoped recommendation records with evidence links + evaluation history, explicit adoption, and machine/human export. **Overlap with #187/#197: reuse memory/knowledge stores instead of a parallel rule store.**
- ACCEPTANCE: [ ] export with scope/evidence/request/evaluation history [ ] inactive until adopted [ ] contradictory/superseded evidence visible [ ] portable machine+human export; no auto harness modification
- FILES: lore/utils/rules.py, lore/storage/knowledge.py, lore/services/improve.py, lore/interfaces/web.py, tests/test_noise_rules.py (new: test_recommendation_export.py)
- DEPENDS-ON: #160, #187, #188, #194 (#44)
- SIZE: L; RISK: format-compat
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_recommendation_export.py -p no:cacheprovider -q -o addopts=` (new)

### #190 Archive #40: message-level semantic retrieval + multilingual evaluation
- STATUS: OPEN. Embeddings are session-level only: lore/storage/session_vectors.py embeds the same title+message-body concatenation as FTS (session_vectors.py:5, :65 `embed_sessions`); no message/chunk vectors, no evaluation set, no calibrated configurable threshold (memory.py:67 mentions a fixed cosine gate for recall fusion). Hybrid/fusion infra exists (lore/storage/fusion.py, tests/test_hybrid_search.py, test_storage_session_vectors.py) but not at message level. External deps #92/#97 noted in body.
- SCOPE: Add message/chunk-level embedding rows with evidence links, a versioned hand-labelled evaluation set (DE/EN/mixed/code/negative), and configurable calibrated thresholds with honest FTS fallback.
- ACCEPTANCE: [ ] late-session passages findable with stable evidence links [ ] multilingual eval measures recall/precision/ranking [ ] calibrated configurable thresholds [ ] honest FTS degradation; no model downloads in unit tests
- FILES: lore/storage/session_vectors.py, lore/storage/embeddings.py, lore/storage/fusion.py, lore/storage/search.py, tests/test_hybrid_search.py, new eval fixture dir
- DEPENDS-ON: #154, #160, #174, #175, #92, #97
- SIZE: L; RISK: privacy (embedding provider choice must respect local-only policy), format-compat
- FIX COMMAND: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_hybrid_search.py tests/test_storage_session_vectors.py tests/test_storage_embeddings.py -p no:cacheprovider -q -o addopts=`

## NOT VERIFIED
- #176 real browser clipboard behavior and CSP output (no browser test harness in repo).
- Exact original acceptance wording of dependencies #151-#170 (Archive #1-#20) and #194/#44, #92, #97 — not fetched (out of group); DEPENDS-ON mappings assume the Archive#N→#(150+N) scheme.
- Whether lazy-loaded session pages re-trigger hljs/enhancement via any global hook — grepped, none found.
