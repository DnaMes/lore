# Docker

The container runs the Lore web UI (Gunicorn, one worker, eight threads) from a
multi-stage image. Nothing in the image is specific to one machine.

## Files

| File | Used by default | Purpose |
| :--- | :--- | :--- |
| `Dockerfile` | yes | Builds the image (`python:3.11.17-slim`, builder and runtime stages) |
| `docker-compose.yml` | yes | Self-contained service: build, port, data volume, limits, health check |
| `docker-compose.sources.yml` | no, opt-in | Runs as your uid, bind-mounts the data directory, and mounts your AI tool history read-only |
| `docker-compose.traefik.example.yml` | no, opt-in | Example Traefik labels, trusted-proxy settings and external network; copy and adapt |
| `.env.example` | no | Template for `.env`, which Compose reads from the project directory |
| `.dockerignore` | yes | Keeps tests, docs, caches, `.env` files and archives out of the build context |

## Verification status

Runtime-verified once, on one Linux host (Docker Engine 29.9.2), in an isolated copy of
the checkout (project name `lore-verify`, host port 5077, container renamed) on
2026-10-26. The default stack was built, started, written to, restarted, read back and
removed with `down -v`. Details are in the table under [Verified behaviour](#verified-behaviour).

Config-checked only (`docker compose ... config`): the sources overlay, the Traefik
overlay, and the default `LORE_BIND` and `LORE_PORT` values. Not run: the sources
overlay and the Traefik overlay as running stacks. Running the sources overlay on a
machine without every listed history directory makes Docker create root-owned empty
directories in your home.

## Quick start

```bash
cp .env.example .env            # optional: set LORE_PORT, LORE_BIND, LORE_PUBLIC_ORIGINS, ...
docker compose up -d
docker compose ps               # wait until the app is "healthy"
```

Open `http://127.0.0.1:5000`. The UI has no login; keep the default loopback bind
unless a reverse proxy in front enforces authentication (see below).

The first build downloads the embedding model (see [Build](#build)).

## Ports

`docker-compose.yml` publishes `${LORE_BIND:-127.0.0.1}:${LORE_PORT:-5000}:5000`.

- `LORE_BIND` defaults to `127.0.0.1`. Set `LORE_BIND=0.0.0.0` only when you intend to
  expose the host, and only behind an authenticating proxy.
- `LORE_PORT` changes the host port; the container always listens on 5000. On macOS
  the AirPlay Receiver often occupies port 5000, so use `LORE_PORT=5001 docker compose up -d`
  and open `http://127.0.0.1:5001`. Set it in `.env` or in the shell.

## Data directory and permissions

By default the data lives in the named volume `lore-data`, mounted at
`/home/ai/.lore`. It holds the SQLite store, exports, memory and caches.

Compose prefixes the volume name with the project name, which defaults to the
checkout directory name: for a checkout in a directory called `lore` the volume is
`lore_lore-data`. Check it with `docker volume ls`.

The image creates user `ai` with UID 10001 and runs as that user. Docker copies the
image's ownership into a new named volume, so the volume is owned by 10001 and
**no manual `chown` is needed**.

To keep the data in a host directory instead, set `LORE_DATA_DIR`:

```bash
LORE_DATA_DIR=/host/path docker compose up -d
```

The bind mount replaces the named volume. `/host/path` must be writable by UID
10001. Otherwise Lore cannot write, and the container still reports `healthy`
because the health check only requests `/api/health`. Fix the ownership with:

```bash
sudo chown -R 10001:10001 /host/path
```

## Tool history (opt-in sources overlay)

By default the container sees only the data directory. To let Lore read your AI
tool history, add the sources overlay. Export your uid and gid first:

```bash
export LORE_UID=$(id -u) LORE_GID=$(id -g)
mkdir -p lore-data              # or the path in LORE_DATA_DIR; must be writable by your uid
docker compose -f docker-compose.yml -f docker-compose.sources.yml up -d
```

The overlay does three things (see `docker-compose.sources.yml`):

1. Sets `user: ${LORE_UID:-10001}:${LORE_GID:-10001}`, so the app runs as your user.
   Host history directories are often `0700`, which UID 10001 cannot read.
2. Replaces the named volume with a bind mount of `${LORE_DATA_DIR:-./lore-data}` at
   `/home/ai/.lore`. That directory must be writable by your uid. Data already in the
   named `lore-data` volume is **not** copied; the overlay starts on the bind mount.
3. Mounts these host directories read-only (`:ro`) under `/home/ai`:
   `.claude`, `.gemini`, `.codex`, `.cursor`, `.config/Cursor`, `.config/Code`,
   `.copilot`, `.local/share/opencode` and `.local/share/opencode/storage`.

Use it only when those paths exist. Docker creates a missing bind source as an empty,
root-owned directory, which Lore then reads as an empty tool history.

## Reverse proxy with Traefik (opt-in example)

`docker-compose.traefik.example.yml` is the only Traefik configuration in the repo.
It is an example; adapt it to your proxy.

1. Create the external network the example expects:

   ```bash
   docker network create traefik_local
   ```

2. Edit `docker-compose.traefik.example.yml`: replace `lore.example.com` in the router
   rule and in `LORE_PUBLIC_ORIGINS=https://lore.example.com` with your host. The
   overlay's value replaces the one from `.env`.
3. Add an authentication middleware (for example basic auth) to the `lore` router.
   Lore has no login of its own.
4. Start the stack:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.traefik.example.yml up -d
   ```

The overlay sets `TRUSTED_PROXY=1`, so Lore trusts `X-Forwarded-*` from the proxy.
Requests whose `Host` is not your public origin, loopback, or in `LORE_ALLOWED_HOSTS`
get a 400.

The overlay does not remove the base file's `ports:` entry, so the UI also answers on
`127.0.0.1:${LORE_PORT:-5000}`. Local access there bypasses the proxy and its auth
middleware. Remove the `ports:` entry in your own override file, or keep `LORE_BIND`
on loopback and accept that local access skips the proxy.

## Environment variables

Compose passes these to the app (defaults shown):

- `FLASK_SECRET_KEY` (empty): passed from `.env` to the app. When it is empty, Lore uses an
  ephemeral key and logs a warning, so browser sessions do not survive a restart. Set a
  stable value in `.env` (which is gitignored):

  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```

- `TRUSTED_PROXY` (empty): passed from `.env`. Only `1`, `true`, `yes` or `on`
  (case-insensitive) makes Lore trust `X-Forwarded-*` headers; `0`, `false`,
  `no`, `off` and any other value disable trust. The Traefik overlay sets it
  to `1`.
- `LORE_PUBLIC_ORIGINS` (empty): public origins, e.g. `https://your.host`. Their hosts
  are allowed in the `Host` header; origins are honoured as same-origin when
  `TRUSTED_PROXY` is set.
- `LORE_ALLOWED_HOSTS` (empty): extra `Host` values, comma-separated.
- `LORE_RATE_LIMIT_ENABLED` (`true`), `LORE_RATE_LIMIT_WINDOW_SECONDS` (`60`),
  `LORE_RATE_LIMIT_MAX_API_REQUESTS` (`240`), `LORE_RATE_LIMIT_SEARCH_PER_WINDOW`
  (`180`), `LORE_RATE_LIMIT_AUDIT_PER_WINDOW` (`20`), `LORE_RATE_LIMIT_RELOAD_PER_WINDOW` (`12`)
- `LORE_JSON_REQUEST_LOGS` (`false`)
- `HOME=/home/ai` (fixed in compose)

Compose itself reads these, not the app: `LORE_BIND`, `LORE_PORT`, `LORE_DATA_DIR`,
`LORE_UID` and `LORE_GID`. They are listed in
[CONFIGURATION.md](./CONFIGURATION.md#docker-compose-variables).

Compose passes no AI provider settings (`LORE_LLM_PROVIDER`, `LORE_ALLOW_CLOUD`, `OLLAMA_HOST`,
provider API keys). Other app settings are in [CONFIGURATION.md](./CONFIGURATION.md).

## Build

```bash
docker compose build app
docker compose up -d app
```

### Build arguments

- `PREFETCH_EMBEDDING_MODEL` (default `on`): **the image build downloads the semantic
  embedding model from Hugging Face by default.** The build imports
  `lore.storage.embeddings`, embeds a warm-up string, and stores the model in
  `/opt/fastembed-cache`. It needs network access to Hugging Face, and the build fails if
  the download fails. Use `off` to skip the download, for example on an offline build host
  or when you do not need semantic search:

  ```bash
  docker compose build --build-arg PREFETCH_EMBEDDING_MODEL=off app
  ```

  With `off` the cache directory is created empty, and the model is fetched on first use if
  the container has network access.

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
loopback request, so it passes the `Host` allowlist without configuration. The check
says nothing about whether the data directory is writable.

## Resource limits and logs

- `cpus: "2.0"`, `mem_limit: 4g`, `pids_limit: 512`
- Logs: `json-file` with `max-size: "10m"` and `max-file: "3"`

## Upgrade

The data volume survives an upgrade: `up -d` recreates the container, not the volume.
Lore applies its own SQLite migrations when the store opens. Take a backup first
(below).

```bash
git pull
docker compose build app        # `docker compose pull` has nothing to pull for this build-only service
docker compose up -d app
docker compose logs -f app
```

## Backup and restore

The SQLite store runs in WAL mode, so copying files while Lore writes can capture an
inconsistent snapshot. Stop the app first. The commands below use the container name
`lore-app` from `docker-compose.yml`; `--volumes-from` reads the named volume through a
throwaway `alpine` container.

Backup (writes the archive to the current directory, atomically):

```bash
docker compose stop app
backup="lore-data-$(date +%F).tar.gz"
tmp="$(mktemp "${backup}.XXXXXX")"
if docker run --rm --volumes-from lore-app alpine \
    tar -C /home/ai/.lore -czf - . > "$tmp"; then
  mv "$tmp" "$backup"
else
  echo "backup failed; an existing backup was not touched"
  rm -f "$tmp"
fi
docker compose start app
```

Restore (replaces the current contents of the volume; keep a copy of the current data first). It verifies the archive in a throwaway container before touching the volume, and it aborts on an unreadable or truncated archive:

```bash
docker compose stop app
# 1. verify the archive without extracting, inside a throwaway container
if docker run --rm -i alpine tar -tzf - > /dev/null < lore-data-2026-10-26.tar.gz; then
  # 2. only then wipe and re-extract
  docker run --rm -i --volumes-from lore-app alpine \
    sh -c 'find /home/ai/.lore -mindepth 1 -delete && tar -C /home/ai/.lore -xzf -' \
    < lore-data-2026-10-26.tar.gz
else
  echo "archive is broken; nothing was changed"
fi
docker compose start app
```

The archive keeps the owner UID 10001, because extraction runs as root in the
throwaway container. For a bind-mounted `LORE_DATA_DIR`, back up the host directory
with `tar` directly after stopping the app.

## Commands

```bash
docker compose logs -f app       # follow logs
docker compose down              # stop and remove the container; the lore-data volume stays
docker compose down -v           # also DELETES the lore-data volume and all archive data
```

## Verified behaviour

Observed on 2026-10-26, one Linux host, Docker Engine 29.9.2, default `docker-compose.yml`
(`docker compose -p lore-verify`, `LORE_PORT=5077`, container renamed to `lore-verify-app`
in a copy of the checkout; the default `lore-app` container was left running):

| Claim | Observed |
| :--- | :--- |
| Default named volume | Mount `type=volume`, name `lore-verify_lore-data`, destination `/home/ai/.lore` |
| User and ownership | `uid=10001(ai)`; `/home/ai/.lore` owned by `10001:10001` with no `chown` |
| Health | `healthy` after start |
| Write, restart, read back | `lore memory add` stored a memory; after `docker compose restart app` it was listed |
| Foreign `Host` header | `GET /api/health` and `/` with `Host: evil.example` return HTTP 400; loopback returns 200 |
| `LORE_PORT` | `docker compose config` publishes `5077` with `LORE_PORT=5077`, `5001` with `LORE_PORT=5001`, `5000` when unset |
| `LORE_BIND` default | `host_ip: 127.0.0.1` when unset |
| Backup and restore | Stop, `tar` through `--volumes-from`, restore with `find -delete` and `tar -x`, start: memory still present, file owner `10001` |
| Recipe re-check on a scratch volume (2026-10-26, `lore-doc-test`) | `mktemp` + `tar -czf -` redirect + `mv` in place only after `tar` succeeds; `tar -tzf -` inside a throwaway `alpine` container accepts a good archive and aborts with `tar: invalid magic` on a corrupt one before the wipe; `find -delete` + `tar -x` restores the seeded file set; scratch volume removed afterwards |
| `LORE_DATA_DIR` on a host directory | Directory mode 755 owned by the test user: writes fail with `Permission denied` while the container still reports `healthy`; mode 777: writes succeed and the file is owned by 10001. `sudo chown` was not tested (no sudo in the test session) |
| Upgrade path | `docker compose pull app` reports "No image to be pulled" (exit 0) |
| `down -v` | Removed the container, the network and `lore-verify_lore-data` |

Not verified: the sources overlay and the Traefik overlay as running stacks, behaviour
on other hosts or Docker versions, and the `sudo chown` fix for a bind mount.

## Related documentation

- [CONFIGURATION.md](./CONFIGURATION.md): environment variables and settings
- [WEB-UI.md](./WEB-UI.md): routes, host and origin checks, security model
- [../SECURITY.md](../SECURITY.md): security policy
