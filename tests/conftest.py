"""Shared pytest fixtures and isolation guards."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Point HOME at an empty directory so no test reads or writes the real one.

    Extractors, ``lore_home()`` and the title cache all resolve ``~``. Without
    this, a web test that rebuilt the index parsed every real Claude/Codex
    session and rewrote the real ``~/.lore/title_cache`` once per session
    (one test ran 33 minutes at 100% CPU). Tests that need their own home set
    ``HOME`` themselves; that runs after this fixture and wins.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _relax_min_user_prompts_for_tests(monkeypatch):
    """Production default MIN_USER_PROMPTS=3 filters out the tiny 1-2 message
    fixtures most extractor tests rely on. The threshold change is a product
    decision, not a test-quality decision — relax it to 1 inside the test
    suite so the existing fixtures stay valid.
    """
    from lore.extractors.base import BaseExtractor

    monkeypatch.setattr(BaseExtractor, "MIN_USER_PROMPTS", 1, raising=True)


@pytest.fixture(autouse=True)
def _reset_embedding_singletons():
    """Reset the embedding-model module singletons between tests.

    `lore.storage.embeddings` caches the loaded model in module-level
    globals (`_model`, `_model_failed`). Without this reset, a test that
    simulates a model-build failure would poison every later embeddings
    test depending on run order. The reset keeps tests order-independent.
    """
    from lore.storage import embeddings

    saved_model = embeddings._model
    saved_failed = embeddings._model_failed
    yield
    embeddings._model = saved_model
    embeddings._model_failed = saved_failed


@pytest.fixture(autouse=True)
def _clear_web_session_filter_cache():
    """Keep the process-wide session-list cache isolated between tests."""
    from lore.interfaces import web

    web._filtered_sorted_session_ids.cache_clear()  # type: ignore[attr-defined]
    yield
    web._filtered_sorted_session_ids.cache_clear()  # type: ignore[attr-defined]
