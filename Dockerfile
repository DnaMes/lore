FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Create the non-root user. Its home is made traversable (0711) so the
# container can also run as the host user via compose's `user:` override —
# the process must be able to *enter* /home/ai to reach the bind-mounted
# /home/ai/.lore data volume. 0711 allows traversal without listing.
RUN useradd -m -u 10001 ai && chmod 0711 /home/ai

# Copy package files
COPY pyproject.toml README.md ./
COPY lore ./lore/
# Top-level CLI entry modules referenced by [project.scripts] / py-modules.
COPY lore_cli.py lore_session_cli.py ./
# COPY preserves the host's directory modes. A checkout restored from a
# backup (rsync -a) or a stricter umask can leave lore/ at 0750; the process
# runs as the non-root `ai` user (or compose's `user:` override) and then
# fails at import with "No module named 'lore.interfaces.web'" — twice now
# (2026-09-15, 2026-09-16). Normalise inside the image so builds do not
# depend on host permissions.
RUN chmod -R a+rX /app/lore /app/pyproject.toml /app/README.md /app/lore_cli.py /app/lore_session_cli.py

# Install dependencies (no postgres or redis — this app uses JSON + SQLite).
# [semantic] pulls fastembed + sqlite-vec so hybrid search works in the
# container (#94); without it search silently degrades to FTS-only.
RUN pip install --no-cache-dir ".[semantic]" gunicorn

# Pre-download the embedding model at build time (#94). fastembed fetches
# bge-small from HuggingFace on first use — an offline container would fail
# that download and silently fall back to FTS. FASTEMBED_CACHE_PATH pins the
# cache to a fixed image path (the default is /tmp, which tmpfs mounts would
# shadow); a+rX keeps it readable when compose overrides `user:`.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
RUN python -c "from lore.storage.embeddings import embed_text; \
    v = embed_text('warmup'); assert v and len(v) == 384, 'model warmup failed'" \
    && chmod -R a+rX /opt/fastembed-cache

# Switch to non-root user
USER ai
ENV HOME=/home/ai

# Set TRUSTED_PROXY=1 when a reverse proxy (nginx, Caddy, Traefik…) sits in
# front of this container. Without it, X-Forwarded-For is ignored so clients
# cannot spoof their IP address. Leave unset for direct exposure.
# ENV TRUSTED_PROXY=1

# Expose port
EXPOSE 5000

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "lore.interfaces.web:app"]
