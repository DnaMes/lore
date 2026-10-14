# Next session prompt — free hosted LLMs in Lore (paste into a fresh Claude Code session)

Written 2026-10-14. Target: Claude Code (current Claude 5 model) in `/home/dnames/projects/ai-stack/lore`.
The prompt is tool-agnostic enough for Codex too; replace "git-ops" with plain git if no wrapper agents exist.

---

## Objective

Finish the "free hosted LLMs in Lore" work: harden the new `jev` provider, verify the `openrouter`
provider against the real endpoint, then build the stage-A search features (query expansion and
rerank) and a small provider fallback chain, so Lore can use free models (Nemotron via OpenRouter,
Jev lanes) for fast, cheap search help and backlog summaries without weakening local-first privacy.

## Context (carry forward)

Who and how:
- The owner delegates decisions. Decide, state the decision in one line, continue. Never hand an
  open option menu back. Reply to the owner in German; write code, comments, docs, commit messages
  and this file's artifacts in English.
- Claude Max weekly limit is at ~96%: route implementation to `builder`, tests to `test-ops`,
  commits to `git-ops`, docs to `doc-ops`/`documenter`. Do not burn the main thread on bulk edits.
- Commit, push and merge into `main` are standing-authorized. Stage explicit paths only. The
  untracked `.parachute/emergency.md` is not yours: leave it alone.

What exists on `main` (all verified, review-gate APPROVE):
- `c86ae91` `lore/llm/openrouter.py`: `OpenRouterProvider` (stdlib urllib, `OPENROUTER_API_KEY`,
  default model `nvidia/nemotron-3-ultra-550b-a55b:free`, override `LORE_OPENROUTER_MODEL`,
  fast alternative `nvidia/nemotron-3.5-lightning:free`, base URL override
  `LORE_OPENROUTER_BASE_URL`). Shared `PROVIDER_CHOICES` / `default_model()` in
  `lore/services/summarize_cli.py`; `_cloud_optin_error()` in `lore_cli.py` requires
  `--allow-cloud` for `openrouter`/`jev` on `analyze`, `knowledge`, `format` (and `summarize`
  via `provider_locality`). Gemini stays the compat default and is not gated there.
- `1127f53` hardening: cache key covers model/base URL/max_tokens/temperature, URLError/OSError/
  TimeoutError retry, https-only base URL (http only for loopback), cache dir 0700 / files 0600.
- `41b74e2` `lore/llm/jev.py`: `JevProvider` shells out to `delegate --type <lane>` with the prompt
  on stdin, empty temp cwd, allowlist `local` and `fastcheap` only (env `LORE_JEV_LANES`).

Facts verified this session (do not re-derive):
- `delegate` lanes `bulk`, `explore`, `go`, `build` run agentic runners with tools (`pi` with
  bash/edit/write, `cmd --yolo`). Session transcripts are untrusted text. NEVER send transcript
  text to those lanes. `--type auto` can pick `agy`; the owner forbids new automation around
  `agy` (Google account policy). Jev provider stays on the text-only allowlist.
- OpenCode Go (`go` lane) expires 2026-10-22 per `docs/roles.md`; do not build on it.
- `delegate "..." --type auto --role summary` worked from the main session and returned in ~11 s;
  inside a delegated builder it fails with "recursive delegation is forbidden". Run live Jev smoke
  tests from the main session only.
- Live Jev smoke from the main session: `get_provider('jev').generate('reply with the single word ok')`
  returned `ok` via `jev/fastcheap` in 9 s (`local` lane was not reachable, fallthrough worked).
- OpenRouter lists `nvidia/nemotron-3-ultra-550b-a55b:free` (1M ctx) and
  `nvidia/nemotron-3.5-lightning:free` (1M ctx); 14 free models in total at 2026-10-14.
- NOT verified: any real OpenRouter call (no key in the session), free-tier rate limits and
  prompt-logging policy, Nemotron "high" reasoning latency on short classification prompts.

Secrets (verified 2026-10-14, values never printed):
- The OpenRouter key lives in 1Password: vault `HomeLab`, item `Hermes: OPENROUTER_API_KEY`
  (id `f3nskhs24ko7izgqoxttkeapqa`, category API_CREDENTIAL, field `credential`, env var name
  `OPENROUTER_API_KEY`). `op` 2.41 is on PATH and was unlocked via the desktop app. Fetch it
  yourself, per command, never into a file, shell history, log, commit or prompt:
  `OPENROUTER_API_KEY="$(op read 'op://HomeLab/f3nskhs24ko7izgqoxttkeapqa/credential')" <command>`
  If `op read` fails (locked vault, no desktop app), report NOT VERIFIED for the live steps and
  continue with offline work; do not ask the owner and do not try other vault items.
- The account is NOT free tier (`GET /api/v1/auth/key` returned `is_free_tier: false`, usage
  about 67 USD, no limit set). A model id without the `:free` suffix costs real money. This is why
  step 2b exists.
- Never copy this key into Lore config, `.env*`, docs, test fixtures or the OpenRouter cache.
- The two `OpenRouter` LOGIN items in the vault are account logins, not API keys: do not read them.

Test baseline (isolated HOME is mandatory on this host):
`HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests -p no:cacheprovider -q -o addopts=`
Last full run: 2649 passed, 25 skipped, plus one known failure
`tests/test_session_summary_card.py::test_absent_summary_renders_at_a_glance_without_provider`
(fails on a clean tree too; deselect it, see step 9). Full suite takes ~11 minutes: run targeted
tests per step and the full suite once before the last commit.
Lint: `.venv/bin/ruff format lore tests lore_cli.py && .venv/bin/ruff check lore tests lore_cli.py`.

## Target State

Ten steps (1, 2, 2b, 3-9), each its own verified unit and commit. Do them in order. After each step: run the
named acceptance commands, report the observed output, then hand the explicit paths to `git-ops`.

### Step 1 — Fix the open jev review notes (one small commit)
Files: `lore/llm/jev.py`, `tests/test_jev_provider.py`, `docs/LLM_INTEGRATION.md` (Jev section only).
1. Cache key: serialize `[system_prompt, prompt, lanes]` with `json.dumps` before hashing (a `|`
   in the prompt currently makes keys ambiguous).
2. A missing `delegate` binary (`FileNotFoundError`/`OSError` from `subprocess.run`) must become a
   `JevError` listing the lane, not a raw exception. Add a test.
3. `LLMResponse.model` and `get_model_info()["model"]` must reflect the configured lanes
   (`LORE_JEV_LANES`), not the constant label.
4. Cache files written 0600 in a 0700 directory, written atomically (temp file + `os.replace`).
   Reuse the permission logic from `openrouter.py`; extract a shared helper only if it removes
   real duplication, otherwise copy the three lines.
5. Document in the Jev section that `max_tokens` and `temperature` are ignored (the broker decides).
Acceptance: `pytest tests/test_jev_provider.py tests/test_cli_openrouter_optin.py -q` green; ruff clean.

### Step 2 — Provider timeout configuration (one small commit)
Lore's default `LLMConfig.timeout` is 60 s; Jev hops can take 9–180 s. Add one env var
`LORE_LLM_TIMEOUT` (seconds) that overrides the default timeout in `get_provider()` for
`openrouter` and `jev` only; Jev default 180, OpenRouter default 90. Document it. Test: env var is
honored and an invalid value falls back to the default with a stderr warning.

### Step 2b — Free-only guard (one small commit, do BEFORE any live call)
The shared account is paid, so the provider must make accidental spend impossible.
1. `OpenRouterProvider` refuses any model id that does not end in `:free` with an
   `OpenRouterError` before a request is made, unless env `LORE_OPENROUTER_ALLOW_PAID=1` is set.
   The check also applies to `LORE_OPENROUTER_MODEL` and the `--model` flag.
2. When the response carries `usage.cost` (or any non-zero cost field), log one stderr warning
   with the amount; with `:free` models it must be 0.
3. Tests (no network): `:free` accepted; `openai/gpt-5.6-luna` and `nvidia/nemotron-3-ultra-550b-a55b`
   (no suffix) rejected; override env accepted; error message contains no key.
4. Document in `docs/LLM_INTEGRATION.md`: free-only by default, how to opt into paid models.

### Step 3 — Live verification of OpenRouter (no commit unless a bug is found)
Fetch the key from 1Password per command (see Secrets) and run it only after step 2b is merged.
If `op read` fails, write NOT VERIFIED in the final report and continue; never write a key to a
file. Commands (main session, redacted prompts only, no transcripts):
```bash
OPENROUTER_API_KEY="$(op read 'op://HomeLab/f3nskhs24ko7izgqoxttkeapqa/credential')" \
.venv/bin/python - <<'EOF'
import time
from lore.llm import get_provider
for model in ("nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-lightning:free"):
    p = get_provider("openrouter", model=model)
    t = time.time()
    r = p.generate("Classify as bug, feature or question, answer with one word: 'login fails with 500'")
    print(model, repr(r.content), round(time.time() - t, 1), "s")
EOF
```
Record latency and any 429/empty-content behavior in `docs/LLM_INTEGRATION.md` under a
"Measured" subsection with the date. If free models return reasoning-only or empty content on
short prompts, fix the provider (fall back to `message.reasoning` only if it is clearly the final
answer; otherwise raise) and add a regression test. Check
https://openrouter.ai/docs/api-reference/limits for the free-tier limits and cite the numbers you
actually read; do not guess them.

### Step 4 — Stage A, part 1: query expansion (one commit)
Goal: a search that also matches German/English synonyms and alternative phrasings, sending ONLY
the user's query to the model (no transcript text), so it needs no `--allow-cloud`.
Files to read first: `lore/services/index.py` (`search_index`, line ~598), `lore/storage/search.py`
(`search_sessions`, `phrase_of`), `lore/storage/fusion.py` (`rrf_merge`), `docs/PLAN-hybrid-search.md`,
`tests/test_hybrid_search.py`, `tests/test_storage_fusion.py`.
Build:
1. New `lore/services/query_expansion.py` with
   `expand_query(query: str, provider: LLMProvider, *, max_variants: int = 3) -> list[str]`.
   Prompt: ask for a JSON array of at most 3 short alternative phrasings (translate DE<->EN,
   synonyms, identifiers/paths/function names kept verbatim). Redact the query with
   `prepare_prompt` first. Parse strictly (`json.loads`, list of str); cap each variant at 80
   characters and the list at `max_variants`; drop empties, duplicates and the original query.
   The model output is untrusted: variants go through the SAME FTS query sanitizer
   (`phrase_of` or whatever `search_sessions` applies to a user query), never raw into FTS5.
   Any exception, timeout (5 s hard cap via a thread/future or provider timeout), unparsable
   output or unavailable provider returns `[]` (fail open: search behaves exactly as before).
2. Integrate in `search_index`: when expansion is enabled and returns variants, run the existing
   search once per query (original first), then fuse the lists with `rrf_merge`. Keep result shape
   (`[{"session": ..., "score": ...}]`) and all existing filters (tool, project, scope, deleted,
   automated) unchanged.
3. Enablement is opt-in and off by default: CLI flag `lore search --expand`, env
   `LORE_SEARCH_EXPAND=1`, provider from `LORE_SEARCH_LLM_PROVIDER` (`openrouter` or `jev`; unset
   means disabled, no cloud default). MCP and web search read the env var only. Print one stderr
   line naming the provider when expansion actually ran.
4. Cache expansions on disk keyed by normalized query + provider + model (the query is not
   transcript text), TTL 30 days, same 0600/0700 rules.
Tests (no network, fake provider): variants fused and original ranked first on ties; fail-open on
exception/timeout/garbage JSON/non-list JSON; variant cap and length cap; FTS operator characters
in a variant cannot break the query; disabled by default (provider never called); deleted and
automated filters still applied; `--expand` without a provider configured prints a clear message
and searches normally.
Acceptance: `pytest tests/test_hybrid_search.py tests/test_storage_fusion.py tests/test_storage_search.py tests/test_cli_search_routing.py tests/test_query_expansion.py -q`.

### Step 5 — Stage A, part 2: rerank top results (one commit)
Rerank sends result snippets, which ARE transcript text, so unlike expansion it MUST require
`--allow-cloud` (or a local provider) exactly like `lore summarize`. This corrects the earlier
sketch that treated snippets as query-only data.
1. `lore/services/rerank.py`: `rerank(query, results, provider, *, top_n=15) -> list[dict]`.
   Send per hit only: index number, title, project, and a snippet capped at 200 characters, all
   passed through `prepare_prompt` redaction. Ask for a JSON array of index numbers best-first.
   Only the first `top_n` hits are sent and reranked; the remaining hits keep their order after them.
   Indices are 1-based, exactly as numbered in the prompt. Validate per entry: keep only real ints
   (reject bools, floats, strings) that are in range, drop out-of-range and duplicate entries, then
   append any of the `top_n` hits the model did not mention in original order. If the output is not
   parseable JSON, not a list, empty, contains no valid entry, or the call fails or times out,
   return the input order unchanged (fail open).
2. Wire behind `lore search --rerank` + `--allow-cloud` (cloud providers) via the same
   `provider_locality` check; env `LORE_SEARCH_RERANK=1` is honored only together with an explicit
   provider choice and `LORE_ALLOW_CLOUD=1`-style opt-in already used elsewhere (reuse, do not
   invent a second mechanism; if none exists, CLI flag only).
3. Tests: reorder applied; invalid indexes ignored; duplicates collapsed; cloud provider without
   opt-in exits 2 and sends nothing; redaction applied to snippets (assert the secret never reaches
   the fake provider).

### Step 6 — Fallback chain (one small commit, only if steps 3/4 justify it)
`lore/llm/fallback.py`: `FallbackProvider(providers: list[LLMProvider])` tries members in order on
provider errors (`OpenRouterError`, `JevError`, timeouts), returns the first success, raises one
error listing per-member reasons without prompt text. Locality of the chain is cloud when any
member is cloud. Env `LORE_LLM_FALLBACK=openrouter,jev` (names validated against
`PROVIDER_CHOICES`). Never falls back to a metered API and never adds `agy`. Skip this step and
say so if measurements in step 3 show no 429/empty-content problems.

### Step 7 — Free-tier call budget for backlog summaries (one commit)
`lore summarize --provider openrouter` over a large backlog must not hammer a free tier. Add a
per-day counter in `lore_home()/llm_budget.json` (0600) with env `LORE_OPENROUTER_DAILY_MAX`
(default from the limit you read in step 3, otherwise 50) and stop cleanly with exit code 0 and a
message naming how many sessions remain. Tests with a fake clock.

### Step 8 — Docs and handoff (doc-ops lane, one docs commit)
Update `docs/LLM_INTEGRATION.md` (providers table, env vars, privacy tiers A/B/C, measured
latencies), `docs/TODO.md` (open items with a recommended decision, no questions to the owner),
and the newest section at the top of `HANDOFF.md`. Add a short Obsidian note under
`~/ObsidianVault/2-Projects/ai-stack/lore/` using the `obsidian-keeper` skill conventions.

### Step 9 — Pre-existing failing test (separate commit, `debugger` lane)
`tests/test_session_summary_card.py::test_absent_summary_renders_at_a_glance_without_provider`
fails on a clean tree. Diagnose the root cause (do not weaken the assertion), fix it, run the
file, and ship it as its own `fix:` commit.

## Scope
- Work only in: `lore/llm/`, `lore/services/` (new `query_expansion.py`, `rerank.py`; edits in
  `index.py`, `summarize_cli.py`), `lore_cli.py`, `tests/`, `docs/`, `HANDOFF.md`, and the Obsidian
  project folder named in step 8.
- Do NOT touch: `lore/extractors/`, export formats (users depend on them), `.env*`, any secret
  store, the `lore/` package name, `.parachute/emergency.md`.

## Constraints
- Python 3.11+, type hints on signatures, `ruff format` + `ruff check`, 100-char lines,
  specific exceptions only, no `from X import *`, no new third-party dependencies (stdlib urllib).
- Transcript text goes only to providers the owner opted into per call (`--allow-cloud`) and
  always after `prepare_prompt` redaction; the LLM disk cache stays off for transcript prompts.
- Model output is untrusted: parse strictly, cap sizes, never execute or splice raw into queries.
- Only make changes directly requested. Do not add features, abstractions or files beyond these steps.
- Conventional Commits, imperative mood, one logical change per commit, explicit `git add <path>`.

## Acceptance Criteria
- [ ] Every step ends with the named test command green and ruff clean, output quoted in the report.
- [ ] `lore search "query"` without flags behaves byte-identically to before (existing tests green).
- [ ] No transcript text reaches a cloud provider without `--allow-cloud`; asserted by tests.
- [ ] No code path sends transcripts to `bulk`/`explore`/`go`/`auto`/`agy` lanes; asserted by tests.
- [ ] Final report lists each requirement with command and observed output, or NOT VERIFIED.

## Action Boundaries
- Proceed with reversible in-scope inspection, edits, tests and commits through `git-ops`.
- Stop and report (do not ask the owner a menu) when: the review gate BLOCKs the same finding
  four times; a live call would spend metered credit; a step needs a decision only the owner can
  make (personal taste or money beyond the daily budgets). Otherwise decide and continue.
- Never force-push `main`, never skip hooks, never print or store `OPENROUTER_API_KEY`.

## Progress Evidence
After each step output: `✅ step N: <what changed>, <test command>, <observed result>, <commit sha>`.
Ground every claim in a tool result.

## Session Strategy
New session. Read `AGENTS.md`, the top section of `HANDOFF.md`, `docs/LLM_INTEGRATION.md` and this
file first. Delegate independent, bounded tracks (steps 1+2 are independent of step 4) to
`builder`; keep the main thread for verification and the live smoke tests.

This prompt is for an agentic tool with real system access. Review the scope locks, forbidden
actions, and stop conditions before pasting. Confirm file paths, directories, and permissions match
the actual project.
