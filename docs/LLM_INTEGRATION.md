# AI History LLM Integration

This module provides LLM-powered features for analyzing and processing your AI coding sessions.

## Features

### 1. `lore analyze` - Statistics & Insights

Generate AI-powered statistics and insights about your coding sessions.

```bash
# Basic stats (no LLM required)
lore analyze --no-llm

# AI-powered insights (requires Gemini API key)
lore analyze

# Use specific model
lore analyze --model gemini-2.0-flash

# Save to specific file
lore analyze --output ~/my-stats.json
```

### 2. `lore knowledge` - Knowledge Extraction

Extract structured knowledge from your sessions using LLM.

```bash
# Extract knowledge from last 50 sessions
lore knowledge

# Extract from specific tool
lore knowledge --tool opencode

# Limit to 20 sessions
lore knowledge --limit 20
```

The conversation text of each session is sent to the configured provider (the
provider is named before anything is sent). Results are stored per session in
`session_knowledge`; sessions unchanged since the last run are skipped, and
deleted sessions are excluded. `--tool` also scopes the printed output. The command
exits 1 when every provider call fails.

### 3. `lore improve` - Memory candidate pipeline

Runs ordered stages: backfill memory embeddings, optional knowledge extraction,
deterministic promotion of stored knowledge into memory candidates, and proposals
from session summaries. Model output can only create `candidate` rows for review.

```bash
# Local-only stages (no LLM, no embedding backend): reports skip reasons, exits 0
lore improve

# Run one stage, dry-run counts, or machine-readable report
lore improve --stage distill_lessons --dry-run
lore improve --json

# LLM extraction when a provider is configured (no cloud default)
LORE_LLM_PROVIDER=ollama lore improve --provider ollama
```

The `extract_knowledge` stage uses `--provider` or `LORE_LLM_PROVIDER`; if neither
is set it is skipped with `no_llm`. A non-local provider also needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`; without it the command exits 2 and sends nothing. Exit code 1 only when a stage errors.

### 4. `lore summarize` - AI session summaries

Generate structured per-session summaries (goal, outcome, decisions with cites) from
archived transcript data. Rows live in the v2 store (`session_summaries`); the web UI
reads them later.

```bash
# Local-first default (Ollama); estimate cost first
lore summarize --dry-run

# One session (full id or unique prefix)
lore summarize --session abc123

# Cloud provider — transcript text leaves the machine; requires opt-in
lore summarize --provider gemini --allow-cloud --max-sessions 5
```

Default provider is **Ollama** (local). Cloud providers require `--allow-cloud` or the
command exits without sending anything. Prompts are redacted before send; transcript tasks
disable the LLM disk cache. Use `--dry-run` to preview token/call estimates. Summaries can
still be wrong or costly on large backlogs — cap with `--max-sessions` and `--max-input-tokens`.

### 4. `lore format` - Session Formatting

Format sessions with AI-generated summaries and tags.

```bash
# Format last 10 sessions
lore format

# Format specific session
lore format --session-id abc123

# Format sessions from specific tool
lore format --tool claude-code --limit 20
```

### 5. Archived body and analysis coverage (#177, #178)

Every AI task above reads one canonical body: the archived message rows of the v2
store, in store (`seq`) order, with the full content. `lore summarize`, `lore
knowledge`, `lore format` and the improvement pipeline all go through
`lore.services.hydration` — a session's index metadata or a lossy live excerpt is
replaced, never analysed.

Hydration is explicit about failure. `HydrationError.reason` is one of a closed set,
and a task reports it instead of prompting with an empty conversation:

| Reason | Meaning |
|--------|---------|
| `no-v2-store` | the output directory has no v2 SQLite store |
| `session-not-archived` | the store has no row for that session id |
| `messages-not-synced` | the row exists but its message rows were never written |
| `empty-transcript` | the row is marked synced but has zero message rows |

`lore knowledge` counts skips per reason (`hydration_reasons`, printed as e.g.
`1 messages-not-synced, 1 session-not-archived`); `lore format` prints
`Skipped <id>: <reason>` per session, summarises the rest, and exits 1 when nothing
could be hydrated. The formatted page and the knowledge entry both record the
archived revision (`source_revision`) they were produced from. With AI disabled,
`SessionFormatter(..., include_summary=False)`/an unavailable provider still renders
the complete body — formatting and export never depend on a provider.

Long sessions are summarised in bounded, ordered chunks. The number of chunks is
derived from the token budget (`--max-session-tokens`) instead of a hard cap: when
the budget allows, every message is covered; when it does not, sampling is the last
resort and the result says exactly what it skipped. The session summary payload
carries a `coverage` object:

```json
{
  "chunks_total": 12, "chunks_used": 12, "complete": true,
  "omitted_chunks": [], "seq_range": [1, 240],
  "messages_total": 240, "messages_covered": 240, "messages_omitted": 0,
  "omitted_seq_ranges": [], "user_turns_omitted": 0,
  "revision": "<sha256 of the compressed transcript>", "truncated": false
}
```

`complete: false` (and `truncated: true`) always comes with non-empty
`omitted_chunks` and `omitted_seq_ranges`, so a partial summary is never reported as
a complete analysis. `lore summarize --dry-run` uses the same plan as the run, so its
expected call count matches what the run actually sends.

### 6. Message-level retrieval (#190)

A session hit tells you *which* transcript; a long German/English session still
hides *where* the answer is. `lore search QUERY --messages` (API:
`lore.storage.search_messages`) returns the archived messages themselves, one
dict per hit:

```json
{
  "session_id": "claude-code:laptop:sess-1",
  "seq": 41,
  "role": "assistant",
  "score": 0.55,
  "excerpt": "Wenn die Migration fehlschlägt, spiele das Backup zurück …",
  "excerpt_truncated": true,
  "evidence": {"message_id": 812, "message_seq": 41, "message_native_id": "…",
                "message_sha256": "…", "session_namespace": "claude-code:laptop",
                "source_revision_id": "…"},
  "permalink": "/session/claude-code:laptop:sess-1/m/41",
  "source": "message"
}
```

The identity is `(session_id, seq)` — not the SQLite rowid — so `permalink` and
`evidence.message_sha256` survive a reindex, a prune/undo and a restore. An
excerpt with `excerpt_truncated: true` is a window, never the full message.

Two paths, and the result's `path` field names the one that ran:

| `path` | When | What it means |
|--------|------|---------------|
| `vector` | the optional `semantic` extra is installed *and* `lore embed-messages` has populated `message_embeddings` | KNN over per-message vectors; finds a passage by meaning, also late in a session. Neighbours farther than `LORE_MESSAGE_SEMANTIC_MAX_DISTANCE` (default 0.72, calibrated on the labelled set in `tools/calibration/message-retrieval-eval.json`) are dropped, so an unrelated query returns nothing instead of a meaningless nearest neighbour (#92). Set the variable to `0` to disable the cutoff. |
| `fts` | otherwise, or when the vector neighbourhood is empty | Keyword search over message *bodies*: the session FTS index picks bounded candidate sessions, their messages are copied into a throwaway in-memory FTS5 index and ranked there. It can miss a paraphrase, but it never invents a hit. |

`score` is smaller-is-better on both paths but not comparable between them:
L2 distance on `vector`, bm25 on `fts`. `lore search --messages` prints the path
and the excerpt, so a fallback is visible rather than silent.

Populating the vector path is a separate, bounded, resumable step —
`lore embed-messages [--budget SECONDS]` — because embedding every message of
an archive is far more work than indexing the sessions. It is incremental by
`(session_id, seq, model)`: a message that already has a vector from the current
model is skipped, a model change re-embeds it, and an interrupted run resumes on
the next call. Message bodies longer than 20 000 characters are truncated for
embedding (the same cap the session pass uses).

`tools/calibration/calibrate_message_cutoff.py` re-measures the threshold against
the versioned evaluation set (German, English, mixed-language, code-heavy and
decoy sessions with hand-labelled late passages); unit tests never download a
model, and `pytest tests/test_message_retrieval_eval.py` asserts recall of every
labelled passage, zero cross-session false positives and zero hits for the
negative queries.


## Setup

### Authentication Methods

Choose ONE of the following authentication methods:

#### Method 1: API Key (Recommended - Free Tier Available)

1. Get your FREE API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Set environment variable:

```bash
# Add to ~/.bashrc or ~/.zshrc
export GEMINI_API_KEY="your-api-key-here"
```

**Note:** The free tier includes 15 requests per minute and 1 million tokens per day.

#### Method 2: OAuth2 (Google AI Pro Subscription)

**Important:** OAuth tokens from Gemini CLI won't work directly because they lack the Generative Language API scope. You need to create your own OAuth credentials:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable the "Generative Language API"
4. Go to APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
5. Select "Desktop application"
6. Use [OAuth Playground](https://developers.google.com/oauthplayground/) to get refresh token:
   - Click the gear icon → Use your own OAuth credentials
   - Enter your Client ID and Client Secret
   - Select scope: `https://www.googleapis.com/auth/generative-language`
   - Authorize and exchange code for tokens
   - Copy the `refresh_token`

```bash
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"
```

#### Method 3: Application Default Credentials (ADC)

If you have `gcloud` CLI installed and have enabled the Generative Language API:

```bash
# Login with gcloud
gcloud auth application-default login

# The tool will automatically use ADC
```

### Ollama (Local LLM)

1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3.2`
3. Use with `--provider ollama`:

```bash
lore analyze --provider ollama --model llama3.2
```

### OpenRouter provider

Cloud access to many models via [OpenRouter](https://openrouter.ai/). Set
`OPENROUTER_API_KEY`. Optional overrides: `LORE_OPENROUTER_MODEL` (default
`nvidia/nemotron-3-ultra-550b-a55b:free`), `LORE_OPENROUTER_BASE_URL` (default
OpenRouter API v1). Custom base URLs must use `https://`; plain `http://` is
accepted only for loopback hosts (local OpenAI-compatible servers).

A faster free alternative is `nvidia/nemotron-3.5-lightning:free` — set
`LORE_OPENROUTER_MODEL` or pass `--model` explicitly.

**Free-only by default.** The shared account is not on the free tier, so a
non-free model id costs real money. Lore therefore refuses any model id that
does not end in `:free` — before the request is sent — whether it comes from the
default, from `LORE_OPENROUTER_MODEL` or from `--model`. The refusal names the
model and never the API key.

```bash
LORE_OPENROUTER_ALLOW_PAID=1 lore summarize --provider openrouter --allow-cloud ...
```

Set `LORE_OPENROUTER_ALLOW_PAID=1` only when spending credit is intended. Any
response whose `usage.cost` (or top-level `cost`) is non-zero additionally prints
`warning: OpenRouter reported a cost of ...` on stderr; with a `:free` model the
amount is always 0.

**Privacy:** free OpenRouter endpoints may log prompts. Lore only sends archived
transcript text to OpenRouter when you opt in with `--allow-cloud` (e.g. on
`summarize`, `knowledge`, or `format`).

```bash
lore summarize --provider openrouter --allow-cloud
```

#### Measured (2026-10-14)

Live calls from the main session, free models only, redacted one-word prompts:

| Model | Latency | Result |
| ----- | ------- | ------ |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 9.8 s | `bug` (correct label, no reasoning-only or empty output) |
| `nvidia/nemotron-3.5-lightning:free` | 5.5 s | `bug` |

No HTTP 429, no empty content, no cost reported, so no stderr warning fired.

Rate limits read from <https://openrouter.ai/docs/api-reference/limits> on
2026-10-14 (not guessed):

- Free-model variants (`:free`): **20 requests per minute**.
- **50 requests per day** when fewer than 10 credits were purchased all-time,
  **1000 requests per day** at 10 credits or more (the higher ceiling starts one
  credit below the threshold).
- `GET /api/v1/key` → `free_model_daily_requests` reports `{used, limit, remaining}`
  for the current UTC day.
- Observed for this key: `is_free_tier: false`, all-time usage `67.03` USD, no
  per-key limit, `free_model_daily_requests: {used: 40, limit: 1000, remaining: 960}`
  — the 1000/day tier applies.
- On 429, retry with exponential backoff and honour `Retry-After` (the provider
  already does this, `_MAX_RETRIES`).

#### Method 2: OAuth2 (Google AI Pro Subscription)

If you have a Google AI Pro subscription and OAuth tokens:

```bash
# Set OAuth credentials
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"
```

To obtain OAuth tokens:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create OAuth 2.0 credentials (Desktop app)
3. Use OAuth playground to get refresh token:
   - Go to https://developers.google.com/oauthplayground/
   - Select "Gemini API" scopes
   - Authorize and exchange code for tokens
   - Copy the refresh_token

#### Method 3: Application Default Credentials (ADC)

If you have `gcloud` CLI installed:

```bash
# Login with gcloud
gcloud auth application-default login

# The tool will automatically use ADC
```

### Ollama (Local LLM)

1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3.2`
3. Use with `--provider ollama`:

```bash
lore analyze --provider ollama --model llama3.2
```

## Configuration

Create `~/.lore/config.json`:

### API Key Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "api_key": "${GEMINI_API_KEY}",
    "cache_enabled": true
  }
}
```

### OAuth Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "oauth_client_id": "${GOOGLE_CLIENT_ID}",
    "oauth_client_secret": "${GOOGLE_CLIENT_SECRET}",
    "oauth_refresh_token": "${GOOGLE_REFRESH_TOKEN}",
    "cache_enabled": true
  }
}
```

### ADC Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "use_adc": true,
    "cache_enabled": true
  }
}
```

## Timeouts

`LLMConfig` defaults to 60 s per call. The two hosted providers wait longer
because they need it: **jev** walks a delegate lane chain (default **180 s**)
and **openrouter** talks to a free-tier endpoint (default **90 s**).

`LORE_LLM_TIMEOUT` (integer seconds) overrides both — and only those two;
`gemini` and `ollama` keep their defaults so a global variable never changes a
local setup. An explicit `timeout=` argument passed to `get_provider()` wins
over the env var. An unparsable value (empty, `0`, negative, `soon`) is ignored
with a warning on stderr and the provider default applies.

```bash
LORE_LLM_TIMEOUT=240 lore summarize --provider jev --allow-cloud --dry-run
```

## Output Locations

- **Stats**: `~/.lore/stats/stats.json`
- **Knowledge**: `~/.lore/knowledge/knowledge_base.json`
- **Formatted**: `~/.lore/formatted/*.md`

## Caching

LLM responses are cached in `~/.lore/llm_cache/` for tasks that do not send session
transcript text (for example aggregate statistics insights); transcript-bearing tasks such as
`lore knowledge`, `lore summarize`, and session formatting summaries never use the disk
cache, and no environment variable re-enables that caching.

Before any transcript text is sent to a provider, Lore runs prompt redaction so secret-shaped
strings are replaced with markers and are not logged or transmitted.

For Ollama, set `LORE_OLLAMA_NUM_CTX` (default 8192, clamped 2048–131072) to control the
context window passed as `num_ctx` on each chat request.

Cached non-transcript responses help:

- Reduce API costs
- Speed up repeated queries
- Enable offline access to previous results

## Session titles

`lore generate-titles` sends a transcript excerpt (first six messages, capped) to the selected
provider, so the provider is explicit and defaults to **local**:

| Flag | Effect |
| --- | --- |
| `--provider local` (default) | Ollama with `--model` or `phi3:mini`. Nothing leaves the machine. |
| `--provider gemini-cli` | The Gemini CLI on `PATH` — **cloud**, used only when selected here, and it also needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`. |
| `--model NAME` | Model for the selected provider. |

The Gemini CLI is no longer picked because its binary happens to exist: before this change a
SMART/AUTO run silently sent excerpts to it whenever `gemini` was on `PATH`. When the selected
provider does not answer, titles fall back to the *local strategies* (keywords, first message) —
never to a different provider (#185).

Every generated title is recorded in `~/.lore/title_cache/titles.json` (owner-only, written
atomically because it can carry an excerpt) with `provider`, `model`, `prompt_version`, a
120-character `excerpt` of what was sent, and `status` (`native`, `summary`, `llm`, `llm-failed`,
`keywords`, `first_message`, `fallback`). A failed LLM attempt is kept under `llm_failed` next to
the fallback's own record. The `fast` strategy never calls a provider at all, so index builds stay
local and offline.

## Cost Optimization

- Use `--no-llm` for basic stats (free)
- Use `gemini-2.0-flash` (fast and cheap)
- Cache is enabled by default
- Limit sessions with `--limit` flag

## Jev provider

The **jev** provider sends prompts to the ai-stack **delegate** broker using only
text-only lanes (`local`, `fastcheap`). Agentic lanes (`auto`, `bulk`, `explore`, …)
are never used, because session transcripts are untrusted input.

### Requirements

- `delegate` on `PATH` (ai-stack harness)
- For transcript-bearing commands (`lore summarize`, `lore knowledge`, …), pass
  `--allow-cloud` (jev is classified as cloud for locality policy)
- Optional: `LORE_JEV_LANES` — comma-separated lane order (default `local,fastcheap`)

### Example

```bash
lore summarize --provider jev --allow-cloud --dry-run
lore analyze --provider jev --allow-cloud --limit 5
```

System prompts are prefixed to stdin (`system`, blank line, user prompt). The broker
timeout follows `DELEGATE_TIMEOUT_SECONDS` (default 300s) inside `delegate`; Lore’s
timeout wraps each lane attempt — **180 s by default**, overridable with
`LORE_LLM_TIMEOUT` (see [Timeouts](#timeouts)).

`max_tokens` and `temperature` are **ignored** by this provider — the broker decides
the limits per lane, so Lore passes neither through. `LLMResponse.model` is
`jev/<lane>` for the lane that actually answered, and `get_model_info()["model"]`
is the configured lane list (`LORE_JEV_LANES`).

## Caching and the request identity

Provider caches are keyed by `LLMProvider.cache_identity()`: a canonical dict with
the provider class, the effective model, the task, the prompt version, the cache
scope (the source revision or coverage the request was built from), the sampling
parameters (temperature, max tokens, `num_ctx`, provider options, base URL) and
SHA-256 fingerprints of the prompt and system prompt. `OpenRouter` adds the values
it resolves per call, `jev` adds its lane list. Bumping `CACHE_KEY_VERSION`
invalidates every entry at once.

Every entry records that identity under `request`, so an old artifact stays
auditable after the prompt or code moved on. Writes go through a temp file plus
`os.replace` and are restricted to the owner, so concurrent workers never see a
half-written entry, and two providers sharing one cache directory cannot answer
for each other.

Transcript-bearing prompts still disable the cache entirely (`LLMConfig.for_transcripts`);
this key work applies to any provider call that does use it.

## Native CLI adapters

Four provider names are answered by an installed CLI rather than by an API client:
`claude-cli` (#182), `codex-cli` (#181), `gemini-cli` (#184) and
`antigravity-cli` (#183). They live in `lore/llm/adapters/`, and the boundary is
explicit: one persisted request descriptor (`lore/llm/adapters/contract.py` —
provider identity, model, auth mode, timeout, cancellation token, execution
policy), one selection table (`selection.py`), one invocation path
(`native_cli.py`). `lore.llm.factory.get_provider()` builds every provider from
that descriptor, so a name cannot mean one thing in the factory and another in the
adapter.

| Provider | Executable | Invocation | Parsed output |
| --- | --- | --- | --- |
| `claude-cli` | `claude` | `claude --print [--model M]`, prompt on stdin | `--output-format json`: `{type, subtype, is_error, result, session_id, usage}` |
| `codex-cli` | `codex` | `codex exec --json --sandbox read-only --skip-git-repo-check [--model M] -`, prompt on stdin | JSONL events; the answer is the last `agent_message` (`item.completed/item.item_type`, older `msg.type`) |
| `gemini-cli` | `gemini` | `gemini --output-format json -p "<constant instruction>" [--model M]`, prompt on stdin | `{"response": ..., "stats": {...}}` |
| `antigravity-cli` | `antigravity` | none — see below | none |

Select one with the factory (it is a cloud provider, so the opt-in applies):

```bash
export LORE_ALLOW_CLOUD=1   # or pass allow_cloud=True in the library
LORE_LLM_PROVIDER=claude-cli lore improve --stage extract_knowledge --allow-cloud
```

### Availability

An adapter is available only when all three checks pass: `shutil.which` finds the
executable, `<executable> --version` succeeds (the version is parsed and reported,
never guessed), and `<executable> --help` documents the flags the adapter uses
(`--print --output-format` for Claude, `--json` for Codex, `-p --output-format` for
Gemini). `is_available()` is false otherwise, `availability()` returns a `CliProbe`
with `available`, `version`, `capabilities` and a human-readable `reason`, and
`generate()` raises `AdapterUnavailable` naming that reason. **A missing CLI is
never answered by an API provider** — there is no implicit fallback anywhere in the
boundary.

`antigravity-cli` (#183) is availability-gated on purpose: no supported
non-interactive CLI contract could be confirmed offline, so the adapter reports
unavailable with a reason (distinguishing "not installed" from "installed but no
confirmed contract") and invokes nothing. The open questions that block it —
executable name and channel, prompt transport, structured-output format and flag,
credit/limit behaviour, pinnable version range — are listed in the module
docstring `lore/llm/adapters/antigravity.py`. Antigravity conversations stay
archived source artifacts until then.

### Authentication

Native only. The adapter runs the unmodified CLI and lets it use the login it
already has; Lore never reads, copies or forwards a credential:

- No credential file is opened (`~/.claude/.credentials.json`,
  `~/.codex/auth.json`, `~/.gemini/oauth_creds.json` and friends are never touched
  by the adapter).
- `CREDENTIAL_ENV_VARS` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLIENT_ID/SECRET`,
  `GOOGLE_REFRESH_TOKEN`, `OPENROUTER_API_KEY`) are removed from the child
  environment, so only the CLI's own login can answer. If the CLI has no login, the
  call fails with `AdapterAuthError` rather than borrowing a key Lore holds for a
  different provider.
- The rest of the environment is inherited untouched; nothing is added.
- An API key offered to a native CLI is refused
  (`UnsupportedProviderCombination`), and so is a `base_url` for a subprocess
  provider — before anything runs.
- `lore/llm/gemini.py` no longer reads `~/.gemini/oauth_creds.json` for the
  `gemini` API provider (#184): API credentials and the CLI's login stay separate.

### Limits

`ProviderRequest.timeout` bounds each call (default 60 s; `timeout=` or, for the
hosted API providers only, `LORE_LLM_TIMEOUT`). The prompt is bounded by the
execution policy's `max_prompt_chars` (400 000) and refused with
`AdapterPromptTooLarge` beyond it. The child runs in a temporary working directory,
in its own process group, with no shell. The prompt travels on standard input, so
transcript text never appears in the argument vector or the process list.
Sampling limits (`max_tokens`, `temperature`) are not passed: they belong to the
CLI's own configuration and inventing flags for them would be guessing.

### Cancellation

Every request carries a `CancellationToken`
(`lore/llm/adapters/contract.py`). The adapter polls the token while the CLI runs;
cancelling kills the process group and raises `AdapterCancelled`. The same path
enforces the timeout (`AdapterTimeout`). Both leave no orphaned process behind.

Outcomes are distinct and typed, so a caller can persist them separately:
unavailable, auth error, timeout, cancelled, execution error (non-zero exit or a
reported failed result), malformed/partial output (`AdapterOutputError`; a
truncated trailing JSONL line after usable events is reported as partial output,
not as failure).

### Deliberately unsupported

- **Antigravity invocation** until the CLI contract is confirmed (see above).
- **Reusing one provider's credentials for another**: no token, cookie or CLI login
  crosses the boundary in either direction.
- **`--continue`/`--resume`, MCP/tool/permission changes, extensions configuration,
  interactive TUIs, image input** — the adapter is one bounded text call.
- **`<cli> login` on the user's behalf** — authentication is the user's step.
- **Translating or validating model names**: `--model` is passed through as the CLI
  documents it (`sonnet`, a full id, …); an unknown name fails with the CLI's own
  error. With no model named, Lore passes no `--model` and the CLI uses its own
  configured default.
- **Disk caching**: CLI results are not written to `~/.lore/llm_cache` (the requests
  carry transcripts). `cache_identity()` still covers the whole request —
  provider, model, auth mode, timeout, execution policy and the resolved CLI version
  — so two adapters can never answer for each other.
- **CLI `--provider` choice lists**: `lore analyze|knowledge|summarize|format
  --provider` still enumerates `gemini`, `jev`, `ollama`, `openrouter`. The adapters
  are reachable through `get_provider()` and `LORE_LLM_PROVIDER` (e.g. `lore
  improve`); adding them to every command's choice list is a separate CLI change.

