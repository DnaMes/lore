# Multi-stage image for Lore (JSON + SQLite; no Postgres/Redis).
# Pin: concrete 3.11 slim tag (not :latest). Rebuild periodically to pick up patches.
FROM python:3.11.17-slim AS builder

WORKDIR /app

# Package versions float with the Debian slim snapshot; pinning apt packages is brittle.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY lore ./lore/
COPY lore_cli.py lore_session_cli.py ./

# Install from pyproject (versions pinned there). Prefetch embedding model (default on).
# Offline HuggingFace failures must fail the build with a clear message.
# Disable prefetch: --build-arg PREFETCH_EMBEDDING_MODEL=off
ARG PREFETCH_EMBEDDING_MODEL=on
ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
# hadolint ignore=DL3013
RUN chmod -R a+rX /app/lore /app/pyproject.toml /app/README.md \
        /app/lore_cli.py /app/lore_session_cli.py \
    && pip install --no-cache-dir ".[semantic]" gunicorn \
    && if [ "$PREFETCH_EMBEDDING_MODEL" = "on" ]; then \
         if ! python -c "from lore.storage.embeddings import embed_text; \
v = embed_text('warmup'); \
assert v and len(v) == 384, 'model warmup failed'"; then \
           echo "ERROR: Failed to prefetch the embedding model (network/HuggingFace unavailable)."; \
           echo "Retry with network, or rebuild with --build-arg PREFETCH_EMBEDDING_MODEL=off"; \
           exit 1; \
         fi; \
         chmod -R a+rX /opt/fastembed-cache; \
       else \
         echo "Skipping embedding model prefetch (PREFETCH_EMBEDDING_MODEL=$PREFETCH_EMBEDDING_MODEL)"; \
         mkdir -p /opt/fastembed-cache && chmod -R a+rX /opt/fastembed-cache; \
       fi


FROM python:3.11.17-slim AS runtime

WORKDIR /app

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 ai \
    && mkdir -p /home/ai/.lore \
    && chown -R ai:ai /home/ai/.lore \
    && chmod 0711 /home/ai

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app
COPY --from=builder /opt/fastembed-cache /opt/fastembed-cache

ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
ENV HOME=/home/ai

USER ai

# Set TRUSTED_PROXY=1 when a reverse proxy sits in front of this container.
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)"

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", \
     "--no-control-socket", "--access-logfile", "-", "--error-logfile", "-", \
     "lore.interfaces.web:app"]
