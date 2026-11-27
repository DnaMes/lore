#!/usr/bin/env bash
# verify_offline_image.sh — confirm the Docker image can embed offline (issue #101).
#
# Background
# ----------
# The Dockerfile pre-downloads the bge-small embedding model at build time
# (FASTEMBED_CACHE_PATH=/opt/fastembed-cache) so the container can serve
# semantic search without ever hitting the HuggingFace Hub. If the warmup
# layer misses a file the container will silently fall back to FTS — exactly
# what issue #94 was meant to prevent. Issue #101 tracks running the smoke
# test that proves the warmup actually works.
#
# This script automates the verification recipe from the issue body. Run it
# in an environment with working Docker DNS (the local daemon in some setups
# can't resolve deb.debian.org — issue body).
#
# Usage
# -----
#   scripts/verify_offline_image.sh                  # default: lore-app:latest
#   IMAGE=foo:bar scripts/verify_offline_image.sh    # override image tag
#
# Exit codes
# ----------
#   0   offline embed + build-info both reported the semantic backends as
#       available (embeddings_available and sqlite_vec_available both true).
#   1   one of the assertions failed (see output for which).
#   2   docker / curl / jq is missing.
#   3   the image was not built locally — run `docker compose build app` first.
#
# Requires: docker, python3 (used both to drive the offline embed probe and
# to fetch + parse /api/build-info from inside the container — no jq, no
# curl-in-container dependency).

set -euo pipefail

IMAGE="${IMAGE:-lore-app:latest}"

# 1. Sanity-check prerequisites. Fail fast with a clear message rather than
# burying a "command not found" deeper in the script.
for cmd in docker python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "error: '$cmd' is required but not installed" >&2
        exit 2
    fi
done

# 2. The image must exist locally — `docker compose build app` is a
# prerequisite. Without this we get a confusing "no such image" error.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "error: image '$IMAGE' not found locally." >&2
    echo "       run 'docker compose build app' first, then re-run this script." >&2
    exit 3
fi

echo "==> verifying offline embed via --network none (image=$IMAGE)"
# 3. The core smoke test from the issue body. --network none blocks ALL
# egress, so any HF Hub request would fail. If embed_text returns a 384-dim
# vector under that constraint the warmup cache really contains everything
# the model needs. The probe mirrors the exact `python -c` one-liner the
# Dockerfile's own RUN uses — the same code path, so a green result here
# also means the Dockerfile will keep being green on the next build.
docker run --rm --network none \
    -e FLASK_SECRET_KEY=verify-offline-image \
    "$IMAGE" \
    python -c 'from annuli.storage.embeddings import embed_text; v = embed_text("x"); assert v is not None and len(v) == 384, f"embed failed: {v!r}"; print("offline embed OK: dim=", len(v))'

echo "==> verifying /api/build-info reports semantic backends as available"
# 4. Spin up gunicorn in the background, hit /api/build-info, then stop.
# We deliberately avoid publishing a port — the probe only needs to talk
# to the loopback interface inside the container.
CID=$(docker run --rm -d --network none \
    -e FLASK_SECRET_KEY=verify-offline-image \
    "$IMAGE" \
    gunicorn --workers 1 --threads 4 --bind 127.0.0.1:5000 annuli.interfaces.web:app)
cleanup() {
    docker rm -f "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Poll for readiness (gunicorn needs a moment under --network none).
# The python:3.11-slim base image ships curl-less, so drive the probe with
# the same urllib that's already inside the annuli image.
build_info=""
for _ in $(seq 1 30); do
    if build_info=$(docker exec "$CID" python -c '
import urllib.request, sys
try:
    sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:5000/api/build-info", timeout=3).read().decode())
except Exception as exc:
    sys.exit(1)
' 2>/dev/null); then
        break
    fi
    sleep 0.5
done

if [ -z "$build_info" ]; then
    echo "error: gunicorn never became ready inside the container" >&2
    exit 1
fi

# Parse + assert in one tiny Python call so we don't need jq. The expected
# payload shape is documented in annuli/interfaces/web.py::_build_info_payload:
#   { "semantic": { "embeddings_available": bool, "sqlite_vec_available": bool }, ... }
python3 - "$build_info" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
semantic = payload.get("semantic")
if not isinstance(semantic, dict):
    print(f"error: payload has no 'semantic' object: {payload!r}", file=sys.stderr)
    sys.exit(1)
missing = [k for k in ("embeddings_available", "sqlite_vec_available") if k not in semantic]
if missing:
    print(f"error: semantic payload missing keys {missing}: {semantic!r}", file=sys.stderr)
    sys.exit(1)
if not (semantic["embeddings_available"] and semantic["sqlite_vec_available"]):
    print(
        f"error: semantic backends not both available: {semantic!r}",
        file=sys.stderr,
    )
    sys.exit(1)
print(
    "build-info OK:",
    f"embeddings_available={semantic['embeddings_available']}",
    f"sqlite_vec_available={semantic['sqlite_vec_available']}",
)
PY

echo "==> OK: lore-app image works fully offline (issue #101 resolved)"