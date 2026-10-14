# Deployment Guide

## Docker Deployment

### Prerequisites

- Docker and Docker Compose installed
- Access to the project directory

### Building and Deploying

```bash
# Build the Docker image (recommended before deploying)
docker compose build --no-cache

# Deploy/start the stack
docker compose up -d

# Watch logs
docker compose logs -f app

# Check status
docker compose ps
```

### Important Notes

1. **The container name is `lore-app`** (defined in docker-compose.yml)
2. **The image tag is `lore-app:latest`**
3. **Container is healthy** if `docker compose ps` shows `(healthy)` status

### Version Mismatch Warning

The local codebase and container may occasionally get out of sync:

If you see unexpected behavior after pulling updates:

1. Rebuild: `docker compose build --no-cache`
2. Restart: `docker compose restart app`
3. Check container logs: `docker compose logs app`

### Environment Variables

See README.md "Production Ops" section for rate limiting and logging configuration.

### Accessing the Application

- **Local**: http://localhost:5000
- **Deployed (via Traefik or reverse proxy)**: https://your-domain.example.com/

### Behind a reverse proxy

Two environment variables control what the app believes about the client (#193):

| Variable | Effect |
| --- | --- |
| `TRUSTED_PROXY=1` | Read `X-Forwarded-For` / `-Proto` / `-Host` at all. Unset (the default): the headers are ignored, never parsed, never trusted. |
| `LORE_PUBLIC_ORIGINS` | Comma-separated `scheme://host[:port]` origins a browser may send as `Origin` on state-changing requests, e.g. `https://lore.example.com,https://lore.example.com:8443`. Honoured **only** together with `TRUSTED_PROXY`. |

```bash
TRUSTED_PROXY=1 \
LORE_PUBLIC_ORIGINS=https://lore.example.com \
docker compose up -d
```

Scheme and port are part of an origin, so `https://host:8443` never authorises `http://host`
or `https://host`. Entries that are not `scheme://host[:port]` are dropped with a warning.
Without `LORE_PUBLIC_ORIGINS` only loopback origins (`localhost`, `127.0.0.1`, `::1`) pass the
cross-origin guard, so a UI reached through a proxy gets `403` on every state-changing POST
until its origin is listed. While `TRUSTED_PROXY` is set, a state-changing request whose
`X-Forwarded-*` headers do not parse (bad scheme, host or address list) is rejected with `400`
before the origin check.

### Clearing Container State

```bash
# Restart fresh (keeps volumes)
docker compose restart app

# Full rebuild (no cache)
docker compose build --no-cache && docker compose up -d

# Stop everything
docker compose down
```

### Database and Redis

- **db container**: PostgreSQL 15 Alpine (persistent via `db_data` volume)
- **redis container**: Redis Alpine (persistent via `redis_data` volume)

To reset data:

```bash
docker compose down -v  # WARNING: Deletes all data volumes
docker compose up -d
```
