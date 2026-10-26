# Docker

The container runs the Lore web UI (Gunicorn, one worker, eight threads) from a
multi-stage image. Nothing in the image is specific to one machine.

## Files

| File | Used by default | Purpose |
| :--- | :--- | :--- |
| `Dockerfile` | yes | Builds the image (`python:3.11.17-slim`, builder and runtime stages) |
| `docker-compose.yml` | yes | Self-contained service: build, port, data volume, limits, health check |
| `docker-compose.sources.yml` | no, opt-in | Read-only mounts of your AI tool history directories |
| `docker-compose.traefik.example.yml` | no, opt-in | Example Traefik labels and external network; copy and adapt |
| `.env.example` | no | Template for `.env`, which Compose reads from the project directory |
| `.dockerignore` | yes | Keeps tests, docs, caches, `.env` files and archives out of the build context |

## Quick start

```bash
cp .env.example .env            # optional: set LORE_BIND, LORE_DATA_DIR, LORE_PUBLIC_ORIGINS
mkdir -p lore-data              # create it first so you can set its owner (see below)
sudo chown -R 10001:10001 lore-data
docker compose up -d
docker compose ps               # wait until the app is "healthy"
```

Open `http://127.0.0.1:5000`. The UI has no login; keep the default loopback bind
unless a reverse proxy in front enforces authentication (see below).

## Ports

`docker-compose.yml` publishes `${LORE_BIND:-127.0.0.1}:5000:5000`. The default
bind is loopback. Set `LORE_BIND=0.0.0.0` only when you intend to expose the host.

## Data directory and permissions

`${LORE_DATA_DIR:-./lore-data}` is mounted at `/home/ai/.lore`, which holds the
SQLite store, exports, memory, and caches.

The image creates user `ai` with UID 10001 and runs as `USER ai`. The compose file
does not override the user, so the host data directory must be writable by UID
10001. If you let Compose create the directory it is owned by root, and the container
cannot write to it. Create it first and chown it, as in the quick start.

## Tool history (opt-in sources overlay)

By default the container sees only the data directory. To let Lore read your AI
tool history, add the sources overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.sources.yml up -d
```

Every mount is read-only (`:ro`):

- `${HOME}/.claude`, `${HOME}/.gemini`, `${HOME}/.codex`, `${HOME}/.cursor`
- `${HOME}/.config/Cursor`, `${HOME}/.config/Code`, `${HOME}/.copilot`
- `${HOME}/.local/share/opencode` and its `storage` subdirectory

Use it only when those paths exist. Docker creates a missing bind source as an
empty, root-owned directory, which Lore then reads as an empty tool history.

## Reverse proxy with Traefik (opt-in example)

`docker-compose.traefik.example.yml` is the only Traefik configuration in the repo.
It is an example; adapt it to your proxy.

1. Create the external network the example expects:

   ```bash
   docker network create traefik_local
   ```

2. Edit `docker-compose.traefik.example.yml`: replace `lore.example.com` in the
   router rule and in `LORE_PUBLIC_ORIGINS=https://lore.example.com` with your host.
   The overlay's value replaces the one from `.env`.
3. Add an authentication middleware (for example basic auth) to the `lore` router.
   Lore has no login of its own.
4. Start the stack:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.traefik.example.yml up -d
   ```

The overlay sets `TRUSTED_PROXY=1`, so Lore trusts `X-Forwarded-*` from the proxy.
Requests whose `Host` is not your public origin, loopback, or in `LORE_ALLOWED_HOSTS`
get a 400.

The base file still publishes `127.0.0.1:5000`, so anyone on the host can reach the
UI on that port without going through Traefik and its auth middleware. Remove the
`ports:` entry, or keep `LORE_BIND` on loopback and accept that local access bypasses
the proxy.

## Environment variables

Compose passes these to the app (defaults shown):

- `LORE_PUBLIC_ORIGINS` (empty): public origins, e.g. `https://your.host`. Their hosts
  are allowed in the `Host` header; origins are honoured as same-origin when
  `TRUSTED_PROXY=1`.
- `LORE_ALLOWED_HOSTS` (empty): extra `Host` values, comma-separated.
- `LORE_RATE_LIMIT_ENABLED` (`true`), `LORE_RATE_LIMIT_WINDOW_SECONDS` (`60`),
  `LORE_RATE_LIMIT_MAX_API_REQUESTS` (`240`), `LORE_RATE_LIMIT_SEARCH_PER_WINDOW`
  (`180`), `LORE_RATE_LIMIT_AUDIT_PER_WINDOW` (`20`), `LORE_RATE_LIMIT_RELOAD_PER_WINDOW` (`12`)
- `LORE_JSON_REQUEST_LOGS` (`false`)
- `HOME=/home/ai` (fixed in compose)

Read by Compose itself, not by the app:

- `LORE_BIND` (`127.0.0.1`): host address for the published port
- `LORE_DATA_DIR` (`./lore-data`): host path mounted at `/home/ai/.lore`

Other app settings are in [CONFIGURATION.md](./CONFIGURATION.md).

### Flask secret key

If `FLASK_SECRET_KEY` is unset, Lore uses an ephemeral key and logs a warning, so
browser sessions do not survive a restart. Set a stable value in `.env`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## Build

```bash
docker compose build app
docker compose up -d app
```

### Build arguments

- `PREFETCH_EMBEDDING_MODEL` (default `on`): the build imports `lore.storage.embeddings`
  and embeds a warm-up string, which downloads the semantic model from Hugging Face
  into `/opt/fastembed-cache`. Failure fails the build. Use `off` to skip the download
  (for example on an offline build host, or when you do not need semantic search):

  ```bash
  docker compose build --build-arg PREFETCH_EMBEDDING_MODEL=off app
  ```

  With `off` the cache directory is created empty, and the model is fetched on first
  use if the container has network access.

### Base image

Both stages use `python:3.11.17-slim`, a pinned patch tag rather than `latest`. To
pick up security patches, change the tag deliberately, then rebuild.

### Dockerfile behaviour

1. Both stages install `git` and `sqlite3` through apt. The store is SQLite only; there
   is no Postgres or Redis.
2. The builder installs the project with `pip install ".[semantic]" gunicorn`, which
   brings in `fastembed` and `sqlite-vec`.
3. The runtime stage creates user `ai` (UID 10001), copies the installed packages,
   the app, and the model cache, and runs `gunicorn ... lore.interfaces.web:app` on
   `0.0.0.0:5000`.

## Health check

Both the Dockerfile and the compose file define the same check:

```text
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)"
```

Interval 30s, timeout 5s, 3 retries, 20s start period. `/api/health` is a
loopback request, so it passes the `Host` allowlist without configuration.

## Resource limits and logs

- `cpus: "2.0"`, `mem_limit: 4g`, `pids_limit: 512`
- Logs: `json-file` with `max-size: "10m"` and `max-file: "3"`

## Upgrade

Lore applies its own SQLite migrations when the store opens, so an upgrade is a
rebuild and restart. Back up `lore-data` first (below).

```bash
git pull
docker compose build app
docker compose up -d app
docker compose logs -f app
```

## Backup and restore

The SQLite store runs in WAL mode, so copying files while Lore writes can capture an
inconsistent snapshot. Stop the app before you copy:

```bash
docker compose stop app
tar -czf "lore-data-$(date +%F).tar.gz" lore-data
docker compose start app
```

Restore by stopping the app, moving the current `lore-data` aside, extracting the
archive in the same location, and starting the app again. Keep the UID 10001
ownership when you extract.

## Commands

```bash
docker compose logs -f app       # follow logs
docker compose down              # stop and remove the container; lore-data stays on the host
```

## Verification status

`docker compose -f <files> config` was run for the default file, the sources
overlay, and the Traefik example; all three parse. The image was not built and no
container was started for this document, so the build, health check, and UID 10001
write access are NOT VERIFIED here.

## Related documentation

- [CONFIGURATION.md](./CONFIGURATION.md): environment variables and settings
- [WEB-UI.md](./WEB-UI.md): routes, host and origin checks, security model
- [../SECURITY.md](../SECURITY.md): security policy
