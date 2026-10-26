# Lore

Lore collects the session histories that AI coding tools already write to disk and
puts them into one local, searchable archive. It reads Claude Code, Codex, Gemini
CLI, Cursor and 17 more tools, normalizes their transcripts, and keeps the result
in SQLite under `~/.lore`. You can list, search, export and browse that archive in
a local web UI, or query it over MCP from inside an agent. On top of the archive
sits a shared memory store: facts, decisions, lessons and links that any of those
tools can write to and recall later. Everything runs on this machine by default,
and the source directories are only ever read.

## Status

Lore is version 2.4.0. That version is unreleased until it is tagged; the release
notes are in [CHANGELOG.md](CHANGELOG.md). `pyproject.toml` declares the
classifier `Development Status :: 3 - Alpha`.

Stable, used daily by the maintainer:

- install, `lore sync`, `lore export`, `lore list`, `lore search`, `lore stats`
- the local web UI in read mode
- `lore check`

Experimental:

- semantic search, which needs the `semantic` extra
- the shared memory write tools
- the AI analysis commands: `analyze`, `knowledge`, `improve`, `summarize`, `format`
- the MCP write tools

Verified on one host only:

- the Docker setup, default stack (`docker compose up -d`): built, started, written to,
  restarted and read back on one Linux host (Docker Engine 29.9.2). The sources and
  Traefik overlays are checked for configuration only. The image has no built-in login.
  The verification log is in [docs/DOCKER.md](docs/DOCKER.md).

## Quickstart (about 3 minutes)

```bash
pipx install git+https://github.com/DnaMes/lore
lore --version
lore check
lore export --all
lore list --limit 5
lore search "docker"
lore-web   # then open http://127.0.0.1:5000
```

The name `lore` on PyPI belongs to an unrelated project, so the install comes from
the Git repository. Other options (uv tool, virtual environment, editable clone)
are in [docs/INSTALL.md](docs/INSTALL.md); if `lore check` lists no tools, see
Troubleshooting below.

## Supported platforms

CI (the workflow
[.github/workflows/install-matrix.yml](.github/workflows/install-matrix.yml), run on
every push to `main` and every pull request) builds the wheel and runs
`scripts/smoke_install.py` on ubuntu-latest, macos-latest and windows-latest with
Python 3.11, 3.13 and 3.14, plus one job with the `semantic` extra on ubuntu/Python
3.12 and a `pipx install` from Git on ubuntu; `ci.yml` runs the test suite on ubuntu
with Python 3.11, 3.12 and 3.13.

| Platform | What CI verifies | Notes |
| --- | --- | --- |
| Linux (glibc, musl, x86-64; arm64 emulated) | Wheel smoke test on Python 3.11, 3.13, 3.14; `semantic` extra on Python 3.12; `pipx install` from Git | The local Docker matrix `scripts/install-matrix.sh` also covers Python 3.11-3.14 slim images, 3.12 with `semantic`, Alpine/musl, Debian 12, Ubuntu 24.04 and Fedora via distro `pipx`; 10 of 10 targets passed on 2026-10-26 |
| macOS | Wheel smoke test on Python 3.11, 3.13, 3.14 | The smoke test works with synthetic sessions under a temporary HOME |
| Windows | Wheel smoke test on Python 3.11, 3.13, 3.14 | The smoke test works with synthetic sessions under a temporary HOME |

Not verified: real tool histories on macOS or Windows (the smoke test uses synthetic
Claude Code and Codex sessions, so the extractors' default locations for each tool on
macOS and Windows are not exercised), and the `semantic` extra, Python 3.12 and `pipx`
on macOS and Windows, `lore run` with a pseudo-terminal on Windows (it falls back to
running without one), and the Docker image on anything but Linux hosts.

Details: [docs/INSTALL.md](docs/INSTALL.md) (Platforms and verification).

## Supported tools

| Tool | Reads from | Notes |
| --- | --- | --- |
| `claude-code` | `~/.claude/projects/*/*.jsonl`, `subagents/*.jsonl` | JSONL transcripts, subagent runs included |
| `gemini-cli` | `~/.gemini/tmp/*/chats/` | JSONL and JSON session files |
| `codex` | `~/.codex/sessions/`, `~/.codex/archived_sessions/`, scratch copy of `state_5.sqlite` | Archived rollouts may be zstd-compressed |
| `cursor` | Scratch copy of `~/.config/Cursor/User/globalStorage/state.vscdb`; `~/.cursor/projects/*/agent-transcripts/` | Reads its own store, plus agent transcripts |
| `vscode-copilot` | `~/.config/Code/User/workspaceStorage/*/chatSessions/` | JSON and JSONL chat session files |
| `copilot-cli` | `~/.copilot/session-state/*.jsonl` | CLI session state files |
| `opencode` | `~/.local/share/opencode/`, `~/.opencode/` | JSON storage and `opencode.db`, opened read-only |
| `antigravity` | `~/.gemini/antigravity*/brain/*/.../transcript_full.jsonl`, scratch copy of `conversation_summaries.db` | Reads its own store; layout differs between releases |
| `aider` | `~/.aider.chat.history.md` per project | Markdown chat logs |
| `pi` | `~/.pi/agent/sessions/`, `~/.omp/`, `~/.prime/` | Version 3 tree transcripts |
| `command-code` | `~/.commandcode/projects/<slug>/` | JSONL transcripts, metadata and checkpoints |
| `hermes` | Scratch copies of `~/.hermes/state.db` and `~/.hermes/profiles/*/state.db` | Reads its own store |
| `cline` | VS Code `globalStorage/saoudrizwan.claude-dev/tasks/`, `~/.cline/data/sessions/` | Fixture-only coverage |
| `roo-code` | VS Code `globalStorage/rooveterinaryinc.roo-cline/tasks/` | Fixture-only coverage |
| `kilo-code` | VS Code `globalStorage/kilocode.kilo-code/tasks/` | Fixture-only coverage |
| `goose` | Scratch copy of `~/.local/share/goose/sessions/sessions.db` and alternate roots | Fixture-only coverage |
| `crush` | `~/.local/share/crush/projects.json` pointing at `<project>/.crush/crush.db`, scratch copy | Fixture-only coverage |
| `kimi` | `~/.kimi/kimi.json`, `~/.kimi/sessions/<md5>/<session_id>/context.jsonl` | Fixture-only coverage |
| `qwen` | `~/.qwen/projects/<slug>/chats/*.jsonl`, `~/.qwen/tmp/` | Fixture-only coverage |
| `zed` | Scratch copy of `~/.local/share/zed/threads/threads.db` | Reads its own store; fixture-only; zstd needs `zstandard` |
| `factory-droid` | `~/.factory/sessions/<project-slug>/<session-uuid>.jsonl` | Fixture-only coverage |

A tool is listed as `Not found` by `lore check` when its data directory is absent.
Lore reads what the tools already store on disk; it never writes into them.

## Commands at a glance

| Command | What it does |
| --- | --- |
| `lore list` | List the sessions found in the tool directories, filtered by tool, project or date |
| `lore export` | Export sessions to Markdown and rebuild the index |
| `lore search` | Full-text search across archived sessions |
| `lore stats` | Print counts and breakdowns from the existing index |
| `lore digest` | Print an activity digest for a recent window, 7 days by default |
| `lore memory` | Manage shared memory: add, list, search, context, import, dedupe, rate |
| `lore sync` | Sync the sessions of a single tool |
| `lore watch` | Poll for new sessions and export them as they appear |
| `lore check` | Report which tool extractors are available; `--deep` dry-runs them |
| `lore run` | Run a tool and sync its sessions after it exits |
| `lore reindex` | Rebuild the index from all sources on disk |
| `lore-session` | Session manager: list, switch between tools, continue |
| `lore-web` | Serve the web UI on 127.0.0.1:5000 |
| `lore-mcp` | Stdio MCP server, JSON-RPC on stdin and stdout |

Full reference, flags and examples: [docs/CLI.md](docs/CLI.md).

## Web UI

Start it with `lore-web`; it serves `http://127.0.0.1:5000` by default.

The bind is loopback only and the UI has no login. Any other exposure needs a
reverse proxy that enforces authentication and TLS. The Docker stack publishes the
same UI on `${LORE_BIND:-127.0.0.1}:${LORE_PORT:-5000}`.

The `Host` header is checked on every request: it must name `localhost`,
`127.0.0.1` or `[::1]`, the bind host, a host from `LORE_PUBLIC_ORIGINS`, or one
listed in `LORE_ALLOWED_HOSTS` (comma-separated). Other hosts get a 400. Mutating
requests that carry an `Origin` header must match the request's own origin or a
configured public origin, otherwise they get a 403.

Routes, flags and deployment: [docs/WEB-UI.md](docs/WEB-UI.md). Security policy:
[SECURITY.md](SECURITY.md).

## MCP server

`lore-mcp` speaks MCP over stdio and exposes 13 tools: the read tools plus
`memory_write`, `memory_recall`, `memory_feedback`, and `summarize_session`, which
stays disabled until `LORE_MCP_ALLOW_SUMMARIZE=1` is set.

Registration examples for Claude Code, Codex and Cursor, plus the full tool table,
are in [docs/MCP.md](docs/MCP.md).

## Shared memory

`lore memory add`, `search`, `context`, `import-claude` and `dedupe` drive the
store from the shell; `list`, `show` and `rate` complete the command set.

Agents read a budgeted block with
`lore memory context --project /path/to/repo --budget 2000`.

Kinds, statuses, ranking and the import rules: [docs/MEMORY.md](docs/MEMORY.md).

## Privacy

Everything stays on this machine by default: archive, search, web UI, memory and
export.

A provider receives text only when you run an AI command with one. Every command that
can send session text to a non-local provider requires the opt-in, `--allow-cloud` or
`LORE_ALLOW_CLOUD=1`: `analyze`, `knowledge`, `format`, `summarize`, `improve`, and
`generate-titles` with `--provider gemini-cli`. Without it they exit with code 2 and send
nothing. The opt-in is enforced in the library as well as the CLI (`get_provider()` in
`lore/llm/factory.py` and friends raise `CloudOptInRequired`), and `ollama` counts as
local only on a loopback host. `lore knowledge` has no default provider: it exits with
code 2 until you pass `--provider`. `analyze` and `format` default to `gemini`, a cloud
provider; to stay local, pass `--no-llm` (`analyze` only) or `--provider ollama`.
`improve` sends text only when `--provider` or `LORE_LLM_PROVIDER` is set, and
`--dry-run` sends nothing. `generate-titles` uses the local provider by default and needs
no opt-in; the transcript excerpt a title is built from is redacted before it is sent to
any provider. The full per-command provider table is in
[docs/PRIVACY.md](docs/PRIVACY.md).

## Configuration

All settings are `LORE_*` environment variables, listed in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). The data directory is `~/.lore`
and can be changed with `--output-dir`; the tool source directories are read-only.
The cloud opt-in can also be set with `LORE_ALLOW_CLOUD=1` (see docs/CONFIGURATION.md).

## Docker

`docker-compose.yml` is self-contained: `docker compose up -d` builds the local
`Dockerfile` and starts the `app` service as `lore-app`.

- **Build:** the image build downloads the semantic embedding model from Hugging Face
  unless you pass `--build-arg PREFETCH_EMBEDDING_MODEL=off`.
- **Port:** published on `${LORE_BIND:-127.0.0.1}:${LORE_PORT:-5000}`. Loopback only by
  default. Set `LORE_PORT=5001` if port 5000 is taken (macOS AirPlay often uses it).
- **Data:** the named volume `lore-data` is mounted at `/home/ai/.lore`. It is owned by
  the image user (uid 10001), so no `chown` is needed. `LORE_DATA_DIR=/host/path` switches
  to a bind mount; that path must be writable by uid 10001.
- **Tool history** is not mounted by default. For the sources overlay, run
  `export LORE_UID=$(id -u) LORE_GID=$(id -g)` and add `-f docker-compose.sources.yml`. That
  overlay bind-mounts `${LORE_DATA_DIR:-./lore-data}` writable by your uid and mounts your
  AI tool directories read-only.
- **Reverse proxy:** the only Traefik config is `docker-compose.traefik.example.yml`. It
  needs `docker network create traefik_local`, your hostname in the router rule and
  `LORE_PUBLIC_ORIGINS` (both set in that file), and an auth middleware, because Lore has
  no login. The base file still publishes the port on loopback, which skips the proxy.
- **Secret key:** set `FLASK_SECRET_KEY` in `.env`, so sessions survive a restart.

Details, upgrade and backup: [docs/DOCKER.md](docs/DOCKER.md).

## Uninstall

```bash
pipx uninstall lore
```

Deleting `~/.lore` removes the archive, memory and embeddings permanently. The
data in your tool directories is not touched.

## Troubleshooting

- `lore: command not found`: add the binary directory of pipx or your virtual
  environment to `PATH`, or run `pipx ensurepath`.
- A tool shows `Not found` in `lore check`: its data directory does not exist, or
  the tool has not created local sessions yet.
- Claude sessions are missing: older transcripts may have been pruned by the
  tool's retention setting before Lore synced them.
- A session is missing from `lore list` but still found by `lore search`:
  `lore list` reads the live tool directories, not the archive index, so a
  session whose source file was deleted disappears from `lore list` while it
  stays in `lore search`, `lore stats` and the web UI.
- Search returns nothing: build the index first with `lore export --all` or
  `lore sync <tool>`.
- Port 5000 is busy: start the UI with `lore-web --port 5001` or set
  `LORE_WEB_PORT`. For Docker, use `LORE_PORT=5001 docker compose up -d`.
- Semantic search behaves like keyword search: run `pipx inject lore fastembed sqlite-vec`,
  or install the `semantic` extra from the Git repository (PyPI `lore` is a different
  project).

These match the troubleshooting section of [docs/INSTALL.md](docs/INSTALL.md),
which gives the longer explanations.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md): install options, extras, upgrade, uninstall
- [docs/CLI.md](docs/CLI.md): every command with its `--help` output
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): all environment variables
- [docs/MEMORY.md](docs/MEMORY.md): the shared cross-tool memory store
- [docs/MCP.md](docs/MCP.md): MCP tools and client registration
- [docs/WEB-UI.md](docs/WEB-UI.md): routes, API and security model
- [docs/DOCKER.md](docs/DOCKER.md): container setup and host-specific settings
- [docs/PRIVACY.md](docs/PRIVACY.md): read locations, data directory, providers
- [docs/HOWTO.md](docs/HOWTO.md): short recipes for common tasks
- [CHANGELOG.md](CHANGELOG.md): release notes
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and checks
- [SECURITY.md](SECURITY.md): supported versions and vulnerability reporting
- [LICENSE](LICENSE): MIT

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Tests run
with `pytest tests/`; lint and formatting go through `ruff`.

## License

MIT. See [LICENSE](LICENSE).
