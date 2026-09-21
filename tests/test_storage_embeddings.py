"""Tests for the optional semantic-memory layer (#33).

The embedding backend (fastembed) is optional. These tests split into:
- pure-function tests (pack/unpack/cosine) that always run;
- embedding-dependent tests, skipped when the backend is unavailable so
  CI without the 'semantic' extra still passes.
"""

from __future__ import annotations

import pytest

from lore.storage import add_memory, recall_memory
from lore.storage.embeddings import (
    cosine_similarity,
    embeddings_available,
    pack_vector,
    unpack_vector,
)

requires_embeddings = pytest.mark.skipif(
    not embeddings_available(),
    reason="optional 'semantic' extra (fastembed) not installed",
)


# ---------------------------------------------------------------------------
# Pure vector helpers — always run
# ---------------------------------------------------------------------------


def test_pack_unpack_roundtrip():
    vec = [0.1, -0.25, 0.99, 0.0, -1.0]
    restored = unpack_vector(pack_vector(vec))
    assert len(restored) == len(vec)
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, restored))


def test_cosine_identical_vectors():
    assert abs(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-6


def test_cosine_orthogonal_vectors():
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6


def test_cosine_zero_vector_is_safe():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_length_mismatch_is_safe():
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ---------------------------------------------------------------------------
# Graceful degradation — semantic recall must work without the backend
# ---------------------------------------------------------------------------


def test_recall_semantic_falls_back_without_backend(tmp_path, monkeypatch):
    """With embeddings forced unavailable, semantic recall falls back to FTS."""
    import lore.storage.embeddings as emb

    monkeypatch.setattr(emb, "embed_text", lambda _text: None)

    add_memory(tmp_path, "fact", "Kubernetes scaling notes", "HPA autoscales pods")
    # semantic=True must still return the keyword match via FTS fallback.
    hits = recall_memory(tmp_path, "kubernetes", semantic=True)
    assert [h.title for h in hits] == ["Kubernetes scaling notes"]


def test_add_memory_works_without_embeddings(tmp_path, monkeypatch):
    """A missing embedding backend must not block writing memory."""
    import lore.storage.memory as mem

    # Force the embedding store helper to behave as if the backend is absent.
    monkeypatch.setattr(mem, "_store_embedding", lambda *a, **k: None)
    mid = add_memory(tmp_path, "note", "Plain memory", "no embedding here")
    assert mid > 0


# ---------------------------------------------------------------------------
# Real semantic ranking — only with the backend installed
# ---------------------------------------------------------------------------


@requires_embeddings
def test_semantic_recall_ranks_by_meaning(tmp_path):
    """The core of #33: a query finds thematically related memory even when
    no query word appears literally in it."""
    add_memory(
        tmp_path,
        "decision",
        "Postgres 16 for the stack",
        "We standardise on PostgreSQL 16 in Docker.",
    )
    add_memory(tmp_path, "fact", "Cats sleep a lot", "House cats sleep 12-16 hours a day.")
    add_memory(
        tmp_path, "lesson", "Connection pooling", "Database connection pooling cuts query latency."
    )

    hits = recall_memory(tmp_path, "database performance", semantic=True, limit=3)
    titles = [h.title for h in hits]
    # The two DB-related memories must rank above the unrelated cat fact.
    assert titles.index("Cats sleep a lot") == len(titles) - 1


@requires_embeddings
def test_embedding_stored_on_add(tmp_path):
    import sqlite3

    from lore.storage import v2_db_path

    mid = add_memory(tmp_path, "fact", "Embedded memory", "this gets a vector")
    conn = sqlite3.connect(v2_db_path(tmp_path))
    row = conn.execute("SELECT dim FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchone()
    assert row is not None
    assert row[0] > 0  # non-empty embedding dimension


# ---------------------------------------------------------------------------
# #101 — the Dockerfile's offline-warmup layer must cache the model
# ---------------------------------------------------------------------------
#
# `docker compose build app` runs `embed_text('warmup')` once at build time so
# the resulting image can serve semantic search without hitting the HF Hub.
# If the warmup doesn't populate the cache the container silently falls back
# to FTS-only — the exact regression issue #94 fixed and #101 verifies.
#
# This test mirrors that flow against an isolated cache dir: warm once, then
# flip fastembed into "offline" mode (HF_HUB_OFFLINE=1 — fastembed respects
# it via the underlying huggingface_hub client) and re-embed. A passing run
# proves the warmup result is self-contained — which is the same property
# that makes `--network none` work in the real container.
@requires_embeddings
def test_warmup_layer_supports_offline_embed(tmp_path, monkeypatch):
    """#101: the cache populated by the warmup step must suffice offline."""
    import importlib
    import sys

    cache_dir = tmp_path / "fastembed-cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache_dir))

    # Reset the lazily-built singleton so the new cache path takes effect
    # for both the warmup call and the offline call below.
    import lore.storage.embeddings as emb

    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_failed", False)

    # 1. Warmup phase — same code path the Dockerfile RUN executes at build
    # time. The cache directory will end up populated with the bge-small
    # ONNX + tokenizer artifacts.
    warmup_vec = emb.embed_text("warmup")
    assert warmup_vec is not None, "warmup embed returned None — model cache not populated"
    assert len(warmup_vec) == emb.EMBEDDING_DIM
    assert cache_dir.exists(), f"warmup did not create {cache_dir}"
    # fastembed uses huggingface_hub's snapshot layout — a populated cache
    # has at least one model-id subdir. This is the structural property
    # that lets `--network none` work in the container.
    model_subdirs = [p for p in cache_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert model_subdirs, f"no model cached under {cache_dir} after warmup"

    # 2. Offline phase — force fastembed to use only the local cache. This
    # is the same constraint `--network none` imposes in the container.
    # If the warmup missed a file (tokenizer.json, model_optimized.onnx,
    # …) this embed would return None.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_failed", False)
    # Force a fresh module load of fastembed's model manager so the offline
    # flags take effect at the huggingface_hub boundary.
    for mod_name in [
        m for m in list(sys.modules) if m.startswith(("fastembed", "huggingface_hub"))
    ]:
        sys.modules.pop(mod_name, None)
    importlib.import_module("fastembed")
    importlib.import_module("lore.storage.embeddings")  # re-bind module ref

    offline_vec = emb.embed_text("hello offline")
    assert offline_vec is not None, (
        "offline embed returned None — warmup cache incomplete; "
        "container would silently fall back to FTS (#101)"
    )
    assert len(offline_vec) == emb.EMBEDDING_DIM
