# Deployment

Operations page for the running Docker stack. For setup, environment variables and
the opt-in overlays, use [DOCKER.md](./DOCKER.md), the runtime-verified Docker guide.

The stack is a single service `app` built from `Dockerfile` by `docker-compose.yml`.
Compose has no `image:` key; the image is built locally and named after the checkout
directory and service (for a checkout in `lore/`: `lore-app`). The container name
`lore-app` is set in `docker-compose.yml`.

## Day-to-day operations

```bash
docker compose build app       # build the image
docker compose up -d           # start; recreates the container, keeps the lore-data volume
docker compose stop app        # stop
docker compose down            # stop and remove the container; lore-data volume stays
docker compose ps              # status; "(healthy)" once the health check passes
docker compose logs -f app     # follow logs
docker exec lore-app ...       # run a command inside the container (name from compose)
```

`docker compose start app` restarts a stopped container; `docker compose down -v` also
deletes the `lore-data` volume and all data with it.

## Health checks

- `GET /api/health` — liveness. Returns `{"status": "ok", "revision": ..., ...}`; this is the endpoint the Dockerfile and compose health checks request over loopback.
- `GET /api/ready` — readiness. Checks that the output directory is writable and the index parent exists; answers `ready` or `degraded` with per-check details.

## Rebuilding after an update

The named volume `lore-data` (mounted at `/home/ai/.lore`; holds the SQLite store,
exports, memory and caches) survives a rebuild: `up -d` recreates the container, not
the volume. Lore applies its own migrations when the store opens.

```bash
git pull
docker compose build app
docker compose up -d app
docker compose logs -f app
```

## Backup and restore

SQLite runs in WAL mode, so stop the app before copying. The recipe (also in
[DOCKER.md](./DOCKER.md#backup-and-restore)) reads the volume through a throwaway
container via `--volumes-from lore-app`:

```bash
# backup: writes the archive to the current directory, via a temp file
# the archive is only put in place after the tar succeeds
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

# restore: verifies the archive first (a throwaway container lists it),
# and aborts on an unreadable or truncated archive before touching the volume
# replaces the volume contents (keep a copy of the current data first)
docker compose stop app
if docker run --rm -i alpine tar -tzf - > /dev/null < lore-data-YYYY-MM-DD.tar.gz; then
  docker run --rm -i --volumes-from lore-app alpine \
    sh -c 'find /home/ai/.lore -mindepth 1 -delete && tar -C /home/ai/.lore -xzf -' \
    < lore-data-YYYY-MM-DD.tar.gz
else
  echo "archive is broken; nothing was changed"
fi
docker compose start app
```

For a bind-mounted `LORE_DATA_DIR`, tar the host directory directly instead.

## Logs

Logs go to stdout/stderr only; gunicorn writes to the console and the compose
`json-file` driver caps them at 10 MB with 3 rotated files. Read them with
`docker compose logs -f app`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Port busy on start (port 5000 is taken, e.g. by macOS AirPlay Receiver) | `LORE_PORT=5001 docker compose up -d`, or set `LORE_PORT` in `.env` |
| `Permission denied` writing a bind-mounted `LORE_DATA_DIR` | The host path must be writable by container UID 10001: `sudo chown -R 10001:10001 <path>`. The container can still report `healthy`; `/api/ready` shows the failure |
| HTTP 400 behind a reverse proxy (foreign or missing `Host`) | Set `TRUSTED_PROXY=1`, list the public origin in `LORE_PUBLIC_ORIGINS` and/or extra `Host` values in `LORE_ALLOWED_HOSTS` (see [DOCKER.md](./DOCKER.md#reverse-proxy-with-traefik-opt-in-example)) |

See [DOCKER.md](./DOCKER.md) for setup and [CONFIGURATION.md](./CONFIGURATION.md) for
app settings and environment variable reference.
