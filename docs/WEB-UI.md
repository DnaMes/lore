# Web UI

The Lore web interface provides a local dashboard, session inspector, full-text search, and REST API for exploring recorded AI coding sessions across supported tools.

## Start

The web UI can be started using the installed console script or directly via Python from a repository clone:

```bash
# Using installed entry point
lore-web

# Or directly from a clone
python3 lore/cli/web.py
```

By default, the server binds to `127.0.0.1:5000`.

### CLI Flags and Environment Variables

The CLI flags and corresponding environment variables are:

| Flag | Default | Environment Variable | Purpose |
| --- | --- | --- | --- |
| `--host HOST` | `127.0.0.1` | `LORE_WEB_HOST` | Bind host address. |
| `--port PORT` | `5000` | `LORE_WEB_PORT` | Bind port number. |
| `--debug` | `false` | `LORE_WEB_DEBUG` | Runs Flask in debug mode when set to `true`. |

For complete variable descriptions, see [Configuration](./CONFIGURATION.md).

### Production Deployment

The `lore-web` script runs the built-in Flask development server. As noted in the command help text (`lore-web --help`), production environments should run behind a WSGI server such as gunicorn.

The official Docker image (`Dockerfile`) uses the following command:

```bash
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:5000 --access-logfile - --error-logfile - lore.interfaces.web:app
```

## Pages

User-facing interface routes defined in `lore/interfaces/web.py` are grouped into functional views:

### Dashboard and Discovery

- `/`: Main dashboard displaying recent sessions, high-level activity metrics, and search entry point.
- `/sessions`: Paginated session explorer with filtering by tool, project, tag, date range, and free-text search.
  Date filters (`start`, `end`) are interpreted in the host's local timezone, the same
  frame the archive stores. A date-only `end` value covers the whole selected day
  (its last microsecond), so a session recorded at 18:00 on the chosen final day is
  included; a timestamp carrying an offset (`+02:00`, `Z`) is converted to the local
  wall clock instead of being compared naively. The CLI and MCP surfaces use the same
  helper (`lore.utils.datetime.parse_date_bound`, `datetime_in_range`).

  The `tag` filter matches the user's saved tags/bookmarks (the `session_tags`
  table behind `/api/sessions/<id>/tags`) as well as the LLM-generated index
  keywords, and the dropdown lists both. A saved tag is therefore selectable
  immediately after it is set, and it survives a sync, a session rewrite and a
  reindex — the tags are keyed to the session id, not to the derived rows.
- `/projects`: Overview grouping recorded sessions by workspace and repository path.
- `/threads`: Cross-session threads overview for grouped multi-session conversations.
- `/thread/<thread_id>`: Thread detail page presenting an interactive session timeline and message history.
- `/stats`: Activity statistics over the last 30 days (sessions per day/tool/project, message and prompt counts).

### Session Details and Dynamic Fragments

- `/session/<session_id>`: Detailed session view with transcript rendering, tool invocation cards, and metadata.
- `/session/<session_id>/m/<seq>`: Message permalink. Renders that message plus a bounded
  neighbour window (21 messages) straight from the v2 message rows — never the whole
  transcript — so a link into a 20 000-message session stays cheap. The identity is
  `(session_id, seq)`, which survives a reindex, a prune/undo and a restore; an unknown
  session is a 404, a message the store does not know (deleted, or rows not synced yet)
  is a 410 with the reason.
- `/sessions/rows`: HTML fragment of session list table rows for client-side filtering and dynamic pagination.
- `/sessions/<session_id>/peek`: HTML fragment displaying message previews in modal or drawer popups.
- `/session/<session_id>/messages`: HTML fragment for lazy-loading paginated session messages.

### Configuration and Rules

- `/rules`: Overview of active system rules.
- `/memory`: Viewer for persisted agent memory entries.
- `/noise-rules`: Management interface for previewing and editing noise reduction filters.

### Mutating Actions and Exports

- `/session/<session_id>/summary` (`POST`): Triggers asynchronous summary generation for a session.
- `/session/<session_id>/delete` (`POST`): Soft-deletes or hides a session from the index.
- `/sessions/bulk-delete` (`POST`): Deletes multiple sessions in a single operation.
- `/sessions/bulk-tag` (`POST`): Applies tags across multiple selected sessions.
- `/memory/<int:memory_id>/delete` (`POST`): Deletes a specified memory entry.
- `/export/index`: Exports the session index in JSON or Markdown format.
- `/export/<session_id>`: Exports an individual session transcript in JSON, Markdown, or HTML.

## API

The application exposes JSON endpoints under `/api/`. Mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) that carry an `Origin` header must be same-origin or match `LORE_PUBLIC_ORIGINS` with `TRUSTED_PROXY` enabled. Rejected origins receive HTTP 403.

| Purpose | Method | Route | Description |
| --- | --- | --- | --- |
| **Health & Readiness** | `GET` | `/api/health` | Lightweight liveness probe reporting process uptime, version revision, and status. |
| | `GET` | `/api/ready` | Readiness check verifying output directory writability and index file availability (HTTP 200 or 503). |
| **Build Info** | `GET` | `/api/build-info` | Returns build revision, commit SHA, and runtime metadata. |
| **Metrics** | `GET` | `/api/metrics` | Operational counters and process uptime formatted as JSON or Prometheus text (`?format=prom`). |
| **Audit** | `GET` | `/api/audit` | Runs index integrity audit; supports synchronous checks or background jobs (`?async=1`). |
| | `GET` | `/api/audit-status/<job_id>` | Checks status and findings of an asynchronous audit task. |
| **Reload & Cache** | `POST` | `/api/reload-sessions` | Initiates incremental or full reindexing from raw extractor sources (`?async=1`). |
| | `GET` | `/api/reload-status/<job_id>` | Returns execution status of a background reload task. |
| | `POST` | `/api/cache/clear` | Clears in-memory session and index caches, forcing fresh disk reads. |
| | `POST` | `/api/action-cancel/<job_id>` | Cancels an ongoing background reload or audit job. |
| **Tags** | `GET` | `/api/tags` | Lists all user tags and their associated session counts. |
| | `GET` | `/api/sessions/<session_id>/tags` | Retrieves tags assigned to a specific session. |
| | `POST` | `/api/sessions/<session_id>/tags` | Adds a tag to a session. |
| | `DELETE` | `/api/sessions/<session_id>/tags` | Removes a tag from a session. |
| **Find & Summary** | `GET` | `/api/session/<session_id>/find` | Searches within an individual session transcript for text matches. |
| | `GET` | `/api/session/<session_id>/summary` | Returns cached AI-generated summary, model information, and chunk metadata. |
| **Search v1** | `GET` | `/api/search` | Legacy query endpoint returning brief session snippets. Accepts `tool`, `project`, `tag`, `start` and `end`; an invalid date returns 400. |
| | `GET` | `/api/v1/search` | Full-text index search with tool, project, message-role scope, saved-tag (`tag`) and inclusive date (`start`, `end`) filters. Every filter constrains the candidate set before `limit`, so a filtered search still fills its limit from matching sessions. |
| | `GET` | `/api/v1/index/summary` | Compact metadata summary used for rapid dashboard initialization. |
| | `GET` | `/api/v1/sessions` | Paginated session summaries with filtering and query capabilities. |
| | `GET` | `/api/v1/sessions/<session_id>` | Detailed session metadata, supporting live disk fallback (`?live=1`). |
| | `GET` | `/api/v1/sessions/<session_id>/messages` | Paginated message transcript for a specific session. |
| | `GET` | `/api/v1/projects` | Aggregated project summaries with session counts and timestamps. |
| | `GET` | `/api/v1/threads` | Thread overview grouping multi-session conversations. |
| | `GET` | `/api/v1/threads/<thread_id>` | Complete thread details including timeline and message list. |
| **Other API Routes** | `GET` | `/api/stats/costs` | Aggregated token counts and estimated usage costs. |
| | `POST` | `/api/sessions/<session_id>/resume` | Prepares context or command string to resume a session in its CLI tool. |
| | `GET` | `/api/noise-rules` | Returns active message noise reduction rules. |
| | `POST` | `/api/noise-rules` | Saves updated noise reduction rules. |
| | `POST` | `/api/noise-rules/preview` | Previews noise filter normalization against sample message content. |

## Health

The web application exposes two dedicated endpoints for operational checks:

- `/api/health`: Fast process liveness probe. Returns HTTP 200 with JSON payload:
  ```json
  {
    "status": "ok",
    "revision": "dev",
    "uptime_seconds": 120,
    "timestamp": "2026-10-26T12:00:00Z"
  }
  ```
  This endpoint is used by the Docker container healthcheck in `docker-compose.yml`:
  ```python
  import urllib.request

  urllib.request.urlopen("http://127.0.0.1:5000/api/health", timeout=3)
  ```

- `/api/ready`: Deep readiness probe checking that:
  - The storage output directory (`OUTPUT_DIR`, default `~/.lore`) exists and is writable (`os.access(OUTPUT_DIR, os.W_OK)`).
  - The parent directory of the session index file exists.
  - The index file itself is checked (`index_exists`).
  Returns HTTP 200 with `"status": "ready"` if writable and accessible, or HTTP 503 with `"status": "degraded"` along with reported errors if checks fail.

Both endpoints bypass rate limiting.

## Security Model

The security model is based directly on the application implementation:

- **No built-in login**: A search across interface modules (`grep -rn -i "login\|basic_auth\|authenticate\|password" lore/interfaces/web*.py`) yields only one match:
  `lore/interfaces/web_utils.py:79: if parsed.username is not None or parsed.password is not None:`
  This check ensures that configured origins reject embedded credentials. The web interface has no user authentication, login screens, or basic auth checks. See the [Security Policy](../SECURITY.md).
- **Loopback binding by default**: The application binds strictly to `127.0.0.1` out of the box. Exposing the web UI outside the local machine requires placing it behind a reverse proxy (e.g. Caddy, Nginx, or Traefik) that enforces authentication (such as Basic Auth, OAuth2/OIDC, or mTLS) and handles TLS.
- **Origin checks**: Every mutating request (`POST`, `PUT`, `PATCH`, `DELETE`) that carries an `Origin` header is validated before it reaches a route.
  - The origin must match the request's own `scheme://host[:port]`. Scheme, host, and port are compared.
  - Ports in Host and Origin values must be ASCII digits in 1..65535; a value with no port or outside that range (`0`, `65536`, a non-ASCII digit string) is malformed.
  - Origins listed in `LORE_PUBLIC_ORIGINS` (comma-separated `scheme://host[:port]` entries) are also accepted when `TRUSTED_PROXY` is enabled.
  - A malformed or disallowed origin receives HTTP 403. A request without an `Origin` header is not rejected by this check.
  - Without `TRUSTED_PROXY`, forwarded headers are not processed. This prevents forwarded-header spoofing.
- **Host header check**: Every request is validated against a `Host` allowlist before it reaches a route. A disallowed or malformed `Host` receives HTTP 400 (`Bad Host header`). The allowlist contains `localhost`, `127.0.0.1`, `[::1]`, the bind host (`--host` or `LORE_WEB_HOST`), and the host of every `LORE_PUBLIC_ORIGINS` entry. Add extra hosts with `LORE_ALLOWED_HOSTS` (comma-separated `host[:port]` entries). An entry without a port matches any port. The literal `*` disables Host validation and logs a warning.
- **Non-loopback bind warning**: `lore-web` prints a warning to stderr when bound to a non-loopback host without `LORE_ALLOWED_HOSTS` or `LORE_PUBLIC_ORIGINS`.
- **Rate limiting**: Enforced in `prepare_request_context_and_limit()` in `lore/interfaces/web.py` using logic from `lore/interfaces/web_utils.py`.
  - All `/api/*` endpoints are rate limited, except `/api/build-info`, `/api/health`, `/api/ready`, and `/api/live`.
  - Rate limit thresholds per 60-second window (`LORE_RATE_LIMIT_WINDOW_SECONDS`):
    - `/api/reload-sessions`: `LORE_RATE_LIMIT_RELOAD_PER_WINDOW` (default 12)
    - `/api/audit`: `LORE_RATE_LIMIT_AUDIT_PER_WINDOW` (default 20)
    - `/api/search`: `LORE_RATE_LIMIT_SEARCH_PER_WINDOW` (default 180)
    - Other API routes: `LORE_RATE_LIMIT_MAX_API_REQUESTS` (default 240)
  - Rate limiting can be disabled globally via `LORE_RATE_LIMIT_ENABLED=false` (see [Configuration](./CONFIGURATION.md)).
- **Content Security Policy (CSP)**: The server sets a strict `Content-Security-Policy` header:
  `default-src 'self'; script-src 'self' 'nonce-...'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:;`
  Inline JavaScript event handlers (such as `onclick` attributes) were replaced with event delegation (`addEventListener` on container elements), ensuring execution complies with script nonces and eliminating inline script injection vectors.

## Watcher

The web UI includes an optional filesystem watcher to keep session data synchronized:

- Setting `LORE_WATCH=1` (or `true`) activates `_maybe_start_session_watcher()` during startup.
- A background `SessionWatcher` thread monitors extraction source directories across configured tools.
- When file changes occur, the watcher triggers `clear_index_cache()`, allowing subsequent requests to rebuild the index cache lazily.
- The polling frequency defaults to 30 seconds and can be adjusted with `LORE_WATCH_INTERVAL` (in seconds).

## Probe

The repository includes a security and validation test tool for web deployments:

```bash
lore-web-probe --base-url http://127.0.0.1:5000
```

CLI options from `lore/interfaces/live_probe.py`:

- `--base-url BASE_URL`: Target base URL to probe (required).
- `--user USER`: Basic auth username for authenticating through a reverse proxy.
- `--password PASSWORD`: Basic auth password (direct CLI argument; avoid using in script files or shell history).
- `--password-env PASSWORD_ENV`: Environment variable name holding the basic auth password (default: `LORE_WEB_PROBE_PASSWORD`). To prevent credential leaks, pass passwords exclusively via `--password-env` rather than hardcoding credentials.
- `--timeout-seconds TIMEOUT_SECONDS`: Per-request timeout in seconds (default: 20).
- `--timeout-retries TIMEOUT_RETRIES`: Number of retry attempts upon request timeout (default: 1).
- `--json`: Outputs results as structured JSON instead of line-by-line terminal text.

The probe executes standard test cases against the deployment, verifying that malformed queries, invalid parameters, and unauthorized requests return appropriate HTTP 400 or gateway rejection responses.
