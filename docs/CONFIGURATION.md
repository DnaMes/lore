# Configuration

Lore has no config file. Application settings are environment variables. Docker Compose reads its own variables, listed in [Docker Compose variables](#docker-compose-variables); the app does not read them. Where a CLI flag exists for the same setting, the flag overrides the environment variable. The data directory is `~/.lore` unless `--output-dir` is given.

Defaults below are taken from the code. Where a default depends on the call site, the table says so.

## Storage and output

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_HOME_SCAN_DEPTH | 4; 3 for OpenCode | Maximum directory depth for home-marker discovery. Values below 1 are clamped to 1. | lore/utils/home_discovery.py, lore/extractors/opencode.py |
| LORE_HOME_SCAN_LIMIT | 64; 16 for OpenCode | Maximum number of marker hits per discovery. Values below 1 are clamped to 1. | lore/utils/home_discovery.py, lore/extractors/opencode.py |
| LORE_HOME_SCAN_NODE_LIMIT | 25000 | Maximum number of directory nodes visited per discovery. Values below 100 are clamped to 100. | lore/utils/home_discovery.py |
| LORE_HOME_SCAN_TIME_BUDGET_SECONDS | 10.0 | Wall-time budget in seconds for one home-marker discovery. Values below 1.0 are clamped to 1.0. | lore/utils/home_discovery.py |
| LORE_EXPORT_FALLBACK_SCAN | off (unset) | Enables the fallback scan used by export when set to `true`. | lore/interfaces/web.py |
| LORE_CLEAR_OPENCODE_CACHE | off (unset) | Clears the OpenCode cache when set to `true`. | lore/interfaces/web.py, lore/interfaces/web_data.py |
| LORE_USE_V2 | 1 (on) | Reads sessions from the v2 SQLite store. Set to `0`, `false`, `no` or `off` to use the legacy index.json reader. | lore/services/index.py |
| LORE_IMPORT_PROFILE | relaxed | Import thresholds. `strict` requires 3 messages, 2048 content characters and 5 user prompts. Any other value uses the relaxed thresholds. | lore/extractors/base.py |
| LORE_MIN_USER_PROMPTS | 3 (relaxed); 5 (strict) | Overrides the minimum number of user prompts per imported session. Invalid values are ignored with a warning. | lore/extractors/base.py |

## Web UI and security

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_WEB_HOST | 127.0.0.1 | Bind host for the web UI. A CLI flag overrides it. | lore/cli/web.py |
| LORE_WEB_PORT | 5000 | Bind port for the web UI. A CLI flag overrides it. | lore/cli/web.py |
| LORE_WEB_DEBUG | off | Runs Flask in debug mode only when set to `true`. A CLI flag overrides it. | lore/cli/web.py |
| LORE_PUBLIC_ORIGINS | empty (none) | Comma-separated public origins in `scheme://host[:port]` form. Their hosts join the Host allowlist. With `TRUSTED_PROXY` set, they are also accepted as origins for mutating requests. Malformed entries are dropped with a warning. | lore/interfaces/web_utils.py, lore/interfaces/web.py |
| TRUSTED_PROXY | unset (off) | Only `1`, `true`, `yes` or `on` (case-insensitive) makes Lore trust `X-Forwarded-For`, `-Proto` and `-Host` from a reverse proxy, validate them on state-changing requests, and accept `LORE_PUBLIC_ORIGINS` as origins. `0`, `false`, `no`, `off`, the empty string and any other value disable trust. Set it only when a proxy you control sits in front. Docker's Traefik overlay sets it to `1`. | lore/interfaces/web.py, lore/interfaces/web_utils.py (truthy-value check) |
| FLASK_SECRET_KEY | unset (ephemeral key) | Signing key for Flask session cookies. When unset, Lore generates a random key for each start and logs a warning, so browser sessions do not survive a restart. Use a stable value in production. | lore/interfaces/web.py |
| LORE_ALLOWED_HOSTS | empty (none) | Comma-separated extra `Host` values, optionally with ports. Loopback hosts, the bind host, and `LORE_PUBLIC_ORIGINS` hosts are also allowed. `*` disables Host validation and logs a warning. `lore-web` warns on a non-loopback bind without this or `LORE_PUBLIC_ORIGINS`. | lore/interfaces/web_utils.py, lore/interfaces/web.py, lore/cli/web.py |
| LORE_RATE_LIMIT_ENABLED | true | Enables rate limiting. Accepts `1`, `true`, `yes` or `on`. | lore/interfaces/web_utils.py |
| LORE_RATE_LIMIT_WINDOW_SECONDS | 60 | Length of the rate-limit window in seconds. | lore/interfaces/web_utils.py |
| LORE_RATE_LIMIT_MAX_API_REQUESTS | 240 | Maximum API requests per window. | lore/interfaces/web_utils.py |
| LORE_RATE_LIMIT_SEARCH_PER_WINDOW | 180 | Maximum search requests per window. | lore/interfaces/web_utils.py |
| LORE_RATE_LIMIT_AUDIT_PER_WINDOW | 20 | Maximum audit requests per window. | lore/interfaces/web_utils.py |
| LORE_RATE_LIMIT_RELOAD_PER_WINDOW | 12 | Maximum reload requests per window. | lore/interfaces/web_utils.py |
| LORE_BUILD_SHA | dev | Build identifier returned by the web build-revision helper. Takes precedence over LORE_BUILD_REVISION. | lore/interfaces/web.py |
| LORE_BUILD_REVISION | dev | Fallback build identifier, used only when LORE_BUILD_SHA is unset. | lore/interfaces/web.py |
| LORE_WATCH | off | Starts the background session watcher when set to `1`, `true` or `True`. | lore/interfaces/web.py |
| LORE_WATCH_INTERVAL | 30 | Watcher polling interval in seconds. | lore/interfaces/web.py |
| LORE_JSON_REQUEST_LOGS | false | Emits JSON request logs when set to `1`, `true`, `yes` or `on`. | lore/interfaces/web_utils.py |
| LORE_WEB_PROBE_PASSWORD | unset | Basic-auth password for `lore-web-probe`. Read only when `--password-env` is given, and that flag defaults to this variable name. | lore/interfaces/live_probe.py |

## Memory and search

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_HYBRID_SEARCH | 1 (on) | Enables hybrid keyword and vector search. Set to `0`, `false`, `no` or `off` for keyword-only search. | lore/services/index.py |
| LORE_SEMANTIC_MAX_DISTANCE | 0.88 | Maximum semantic distance kept in search results. Set to `0` to turn the cutoff off. | lore/storage/search.py |
| LORE_MEMORY_RECALL_MIN_SIMILARITY | 0.6 | Minimum similarity for memory recall. Values are clamped to 0 to 1; invalid values use the default. | lore/storage/memory.py |
| LORE_SIMILARITY_THRESHOLD | 0.85 | Similarity threshold used for memory matching. | lore/storage/memory.py |
| LORE_MEMORY_VECTOR_SCAN_LIMIT | 20000 | Maximum number of memories scored in one recall. Only positive integers are used. | lore/storage/memory.py |
| LORE_FEEDBACK_INFLUENCE | 0.0 | Weight of user feedback in ranking. Clamped to 0 to 1. Invalid values give 0. | lore/storage/feedback.py |
| LORE_EMBED_BUDGET_SECONDS | 60 | Time budget per embedding pass. `0` or a negative value disables the budget. | lore/storage/session_vectors.py |
| LORE_DEBUG | off | Prints exceptions from the memory-context hook to stderr when set to `1`. | lore/services/memory_context.py |

## AI providers and analysis

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_LLM_PROVIDER | empty (none) | Provider for the `extract_knowledge` stage of `lore improve` when `--provider` is not given. No other command reads it: `knowledge` always needs an explicit `--provider`, and `analyze`, `format` and `generate-titles` have their own defaults. A non-local value needs the cloud opt-in unless `--dry-run` is given. | lore_cli.py (cmd_improve), lore/services/improve.py |
| LORE_LLM_TIMEOUT | openrouter 90 s; jev 180 s | Request timeout override in seconds for openrouter and jev only. Invalid or non-positive values use the provider default with a warning. An explicit `timeout` argument wins. | lore/llm/factory.py |
| LORE_ALLOW_CLOUD | unset (off) | Grants the cloud opt-in for every non-local provider without `--allow-cloud`. Accepts `1`, `true` or `yes`. Non-local means gemini, openrouter, jev, gemini-cli, and ollama when `OLLAMA_HOST` resolves off-host. Applies to `analyze`, `knowledge`, `format`, `summarize`, `improve` and `generate-titles`, and now also covers the session handoff: `lore-session switch`, `lore-session continue` and the MCP `switch_to_tool` when the target tool is `gemini` or `codex`. Without the opt-in the command refuses and exits with code 2 before anything is sent. | lore/services/summarize_cli.py; session handoff: lore_session_cli.py, lore/interfaces/mcp_tools/sessions.py |
| LORE_OPENROUTER_MODEL | nvidia/nemotron-3-ultra-550b-a55b:free | Default OpenRouter model. | lore/llm/openrouter.py, lore/llm/factory.py, lore/services/summarize_cli.py |
| LORE_OPENROUTER_BASE_URL | https://openrouter.ai/api/v1 | OpenRouter API base URL. | lore/llm/openrouter.py |
| LORE_OPENROUTER_ALLOW_PAID | off | Allows OpenRouter models that do not end in `:free` only when set to `1`. | lore/llm/openrouter.py |
| LORE_JEV_LANES | local,fastcheap | Comma-separated lane order for the jev provider. This default is specific to the owner's delegate broker lanes. | lore/llm/jev.py |
| OLLAMA_HOST | unset (loopback) | Ollama endpoint. Lore treats Ollama as local only when this resolves to loopback; an off-host value makes `ollama` a cloud provider that needs the opt-in. The `ollama` client library also reads it to pick the endpoint. Not a `LORE_` variable. | lore/services/summarize_cli.py (locality check); the `ollama` client library |
| LORE_OLLAMA_NUM_CTX | 8192 | Ollama context window size. Clamped to 2048 to 131072. | lore/llm/ollama.py |
| LORE_SUMMARY_PROVIDER | empty (none) | Local provider for session summaries. Only `ollama` enables web summaries. | lore/services/summarize_cli.py |
| LORE_SUMMARY_MODEL | llama3.2 (ollama default) | Model for session summaries. | lore/services/summarize_cli.py |
| LORE_MCP_ALLOW_SUMMARIZE | off | Enables the MCP summarize tool only when set to `1`. | lore/interfaces/mcp_tools/summaries.py |

## Limits and tuning

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_ACTION_JOB_MAX | 256 | Maximum number of retained action jobs. Clamped to at least 32. | lore/interfaces/web_jobs.py |
| LORE_ACTION_JOB_TIMEOUT_SECONDS | 180 | Timeout for one action job in seconds. Clamped to at least 30. | lore/interfaces/web_jobs.py |
| LORE_ACTION_JOB_TTL_SECONDS | 3600 | Time to keep finished action jobs, in seconds. Clamped to at least 60. | lore/interfaces/web_jobs.py |
| LORE_PARSER_REEXTRACT_BUDGET | 150 | Number of version-stale sources one extractor may re-parse per run. `0` re-parses none. A negative value removes the cap. Invalid values use the default. | lore/extractors/base.py |

## Data directory and backups

Lore keeps everything it owns in one directory: `~/.lore`, or the path given by
`--output-dir`. `index_v2.sqlite` in that directory is the store — sessions,
messages, the shared FTS index, memory, tags and annotations; the legacy
`index.json`/`index.sqlite`, the Markdown exports and the title cache sit beside
it.

`lore backup <path>` writes a versioned bundle of that store, and
`lore restore <path>` puts it back (see [CLI.md](./CLI.md#backup)). The bundle
holds a SQLite-consistent snapshot plus a `manifest.json` with the schema and
migration versions, one sha256 per member and the row count of every archived
table, and it is a zip with its own format version. `lore backup verify <path>`
checks it without restoring.

A bundle contains no credentials, tokens or API keys. Credentials are read from
the environment only (next section) and never written into the store, and a
backup reads the store and nothing else — the provider cache under
`~/.lore/llm_cache` is not part of a bundle.

## Provider credentials (not LORE_ variables)

These names are read from the environment. They are never stored in repo files. Do not write their values into documentation, prompts or commits.

- GEMINI_API_KEY (lore/llm/gemini.py)
- GOOGLE_API_KEY (lore/llm/gemini.py)
- GOOGLE_APPLICATION_CREDENTIALS (lore/llm/gemini.py)
- OPENROUTER_API_KEY (lore/llm/openrouter.py)

## Docker Compose variables

These are read by `docker-compose.yml` and `docker-compose.sources.yml` for variable substitution. **The Python app does not read them.** Set them in `.env` or in the shell before `docker compose`.

| Name | Default | Purpose | Read in |
| --- | --- | --- | --- |
| LORE_BIND | 127.0.0.1 | Host address for the published web port. `0.0.0.0` exposes the UI on every host interface; there is no login. | docker-compose.yml |
| LORE_PORT | 5000 | Host port for the published web port. The container always listens on 5000. Change it when 5000 is taken, for example by macOS AirPlay. | docker-compose.yml |
| LORE_DATA_DIR | unset: named volume `lore-data` | Host directory bind-mounted at `/home/ai/.lore`. Must be writable by the container user (UID 10001, or `LORE_UID` with the sources overlay). With the sources overlay the default is `./lore-data`. | docker-compose.yml, docker-compose.sources.yml |
| LORE_UID | 10001 | User id the app container runs as. Only the sources overlay reads it. Export `LORE_UID=$(id -u)` so the container can read your `0700` history directories. | docker-compose.sources.yml |
| LORE_GID | 10001 | Group id the app container runs as. Only the sources overlay reads it. Export `LORE_GID=$(id -g)` alongside `LORE_UID`. | docker-compose.sources.yml |

## Not configuration

LORE_KEYS_STORAGE, LORE_PEEK_STORAGE, LORE_VIEW_KEY and LORE_VIEW_LEGACY are browser localStorage key names used in the web templates. They are not environment variables, so they are not listed above.

LORE_PUBLIC_ORIGINS_ENV and LORE_ALLOWED_HOSTS_ENV are module constants in lore/interfaces/web_utils.py. Their values are the strings `LORE_PUBLIC_ORIGINS` and `LORE_ALLOWED_HOSTS`, which are the environment variables listed above. The constant names themselves are never read from the environment.
