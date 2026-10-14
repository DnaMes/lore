"""Hybrid memory recall (#197): keywords plus meaning, relevance floor, sources.

A fake embedder replaces the real model: texts mentioning a topic word map to
one unit axis, so cosine similarity is exactly 1.0 (same topic) or 0.0.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

import lore.storage.embeddings as emb
from lore.storage import add_memory, recall_memory, supersede_memory
from lore.storage.memory import (
    DEFAULT_RECALL_MIN_SIMILARITY,
    DEFAULT_VECTOR_SCAN_LIMIT,
    recall_min_similarity,
    vector_scan_limit,
)

TOPICS = (("postgres", "database", "relational"), ("sourdough", "bread", "baking"))


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> Optional[List[float]]:
        self.calls += 1
        lowered = text.lower()
        for axis, words in enumerate(TOPICS):
            if any(w in lowered for w in words):
                return [1.0 if i == axis else 0.0 for i in range(len(TOPICS) + 1)]
        return [0.0] * len(TOPICS) + [1.0]


@pytest.fixture
def fake(monkeypatch) -> FakeEmbedder:
    embedder = FakeEmbedder()
    monkeypatch.setattr(emb, "embed_text", embedder)
    monkeypatch.setattr(emb, "embeddings_available", lambda: True)
    monkeypatch.delenv("LORE_MEMORY_RECALL_MIN_SIMILARITY", raising=False)
    return embedder


def _titles(entries) -> List[str]:
    return [e.title for e in entries]


def test_auto_returns_memory_without_vector_via_fts(tmp_path, fake, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(emb, "embed_text", lambda _t: None)
        add_memory(tmp_path, "fact", "Kubernetes autoscaling", "HPA scales pods")
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    hits = recall_memory(tmp_path, "kubernetes")
    assert _titles(hits) == ["Kubernetes autoscaling"]


def test_auto_adds_semantic_match_without_shared_keyword(tmp_path, fake):
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    add_memory(tmp_path, "fact", "Database keyword", "plain words only")
    hits = recall_memory(tmp_path, "database")
    assert set(_titles(hits)) == {"Postgres notes", "Database keyword"}
    # FTS hit keeps its snippet, the semantic-only hit has none.
    by_title = {h.title: h for h in hits}
    assert by_title["Database keyword"].snippet
    assert by_title["Postgres notes"].snippet is None


def test_auto_floor_drops_unrelated_but_explicit_true_keeps_it(tmp_path, fake):
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    add_memory(tmp_path, "fact", "Sourdough starter", "bread baking schedule")
    assert _titles(recall_memory(tmp_path, "database")) == ["Postgres notes"]
    explicit = recall_memory(tmp_path, "database", semantic=True)
    assert set(_titles(explicit)) == {"Postgres notes", "Sourdough starter"}


def test_floor_is_configurable_by_env(tmp_path, fake, monkeypatch):
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    add_memory(tmp_path, "fact", "Sourdough starter", "bread baking schedule")
    monkeypatch.setenv("LORE_MEMORY_RECALL_MIN_SIMILARITY", "0")
    assert len(recall_memory(tmp_path, "database")) == 2


def test_auto_quoted_phrase_stays_keyword_only(tmp_path, fake):
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    add_memory(tmp_path, "fact", "Database keyword", "plain words only")
    assert _titles(recall_memory(tmp_path, '"database keyword"')) == ["Database keyword"]
    assert _titles(recall_memory(tmp_path, '"database"')) == ["Database keyword"]


def test_semantic_false_never_calls_embedder(tmp_path, fake):
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    calls_after_write = fake.calls
    hits = recall_memory(tmp_path, "database", semantic=False)
    assert hits == []
    assert fake.calls == calls_after_write


def test_no_backend_matches_plain_fts(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)
    monkeypatch.setattr(emb, "embed_text", lambda _t: None)
    add_memory(tmp_path, "fact", "Kubernetes autoscaling", "HPA scales pods")
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")
    assert _titles(recall_memory(tmp_path, "kube")) == ["Kubernetes autoscaling"]
    assert recall_memory(tmp_path, "database") == []


def test_semantic_candidates_respect_superseded_and_scope(tmp_path, fake):
    old = add_memory(tmp_path, "fact", "Postgres v1", "relational", scope_project="p1")
    add_memory(tmp_path, "fact", "Postgres elsewhere", "relational", scope_project="p2")
    new = supersede_memory(tmp_path, old, "Postgres v2", "relational")
    hits = recall_memory(tmp_path, "database", scope_project="p1")
    assert [h.id for h in hits] == [new]
    with_old = recall_memory(tmp_path, "database", scope_project="p1", include_superseded=True)
    assert {h.id for h in with_old} == {old, new}


def test_sources_shape_and_no_message_id(tmp_path, fake):
    mid = add_memory(tmp_path, "fact", "Postgres notes", "relational", source_session="s1")
    expected = [{"session_id": "s1", "note": "recorded from this session"}]
    for entry in (
        recall_memory(tmp_path, "postgres", semantic=False)[0],
        recall_memory(tmp_path, "database")[0],
        recall_memory(tmp_path, "database", semantic=True)[0],
        recall_memory(tmp_path, "")[0],
    ):
        assert entry.id == mid
        assert entry.sources == expected
        assert entry.to_dict()["sources"] == expected


def test_memory_without_source_has_empty_sources(tmp_path, fake):
    add_memory(tmp_path, "fact", "Postgres notes", "relational")
    assert recall_memory(tmp_path, "postgres")[0].to_dict()["sources"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_RECALL_MIN_SIMILARITY),
        ("", DEFAULT_RECALL_MIN_SIMILARITY),
        ("banana", DEFAULT_RECALL_MIN_SIMILARITY),
        ("nan", DEFAULT_RECALL_MIN_SIMILARITY),
        ("0.75", 0.75),
        ("7", 1.0),
        ("-2", 0.0),
    ],
)
def test_floor_env_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LORE_MEMORY_RECALL_MIN_SIMILARITY", raising=False)
    else:
        monkeypatch.setenv("LORE_MEMORY_RECALL_MIN_SIMILARITY", raw)
    assert recall_min_similarity() == expected
    # Calibrated 2026-10-14 on 61 labelled pairs — see
    # docs/research/2026-10-14-recall-floor-calibration.md
    assert DEFAULT_RECALL_MIN_SIMILARITY == 0.6


# --- #199: the vector check and the vector scan respect the query -----------


def test_auto_skips_embedder_when_no_vector_matches_the_query_filters(tmp_path, fake):
    """Auto mode must not load the embedder when the filtered candidates have no vector (#199)."""
    add_memory(tmp_path, "fact", "Postgres notes", "relational store")  # gets a vector
    calls_before = fake.calls

    assert recall_memory(tmp_path, "database", kind="rule") == []
    assert fake.calls == calls_before  # no embedder call for a filter without vectors


def test_vector_scan_is_capped(tmp_path, fake, monkeypatch):
    """The Python cosine scan has a bound; beyond it memories are keyword-ranked (#199)."""
    monkeypatch.setenv("LORE_MEMORY_VECTOR_SCAN_LIMIT", "4")
    for i in range(6):
        add_memory(tmp_path, "fact", f"Postgres fact {i}", "relational store")

    scored = {"n": 0}
    real_cosine = emb.cosine_similarity

    def counting_cosine(a, b):
        scored["n"] += 1
        return real_cosine(a, b)

    monkeypatch.setattr(emb, "cosine_similarity", counting_cosine)

    hits = recall_memory(tmp_path, "database", semantic=True)

    assert scored["n"] <= 4  # the scan never exceeds the bound
    assert len(hits) == 4  # exactly the bound's worth of newest memories


def test_vector_filters_apply_before_the_scan_cap(tmp_path, fake, monkeypatch):
    """kind/scope/status filter in SQL, so the bound never wastes itself on excluded rows (#199)."""
    monkeypatch.setenv("LORE_MEMORY_VECTOR_SCAN_LIMIT", "4")
    for i in range(3):  # older ids — pushed out by the bound first
        add_memory(tmp_path, "fact", f"Postgres fact {i}", "relational store")
    for i in range(3):  # newest rows: without an SQL filter they fill the bound
        add_memory(tmp_path, "note", f"Postgres note {i}", "relational store")

    hits = recall_memory(tmp_path, "database", kind="fact", semantic=True)

    assert len(hits) == 3  # all facts survive: filtered before the bound
    assert all(h.kind == "fact" for h in hits)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_VECTOR_SCAN_LIMIT),
        ("", DEFAULT_VECTOR_SCAN_LIMIT),
        ("banana", DEFAULT_VECTOR_SCAN_LIMIT),
        ("0", DEFAULT_VECTOR_SCAN_LIMIT),
        ("-5", DEFAULT_VECTOR_SCAN_LIMIT),
        ("42", 42),
    ],
)
def test_vector_scan_limit_env_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LORE_MEMORY_VECTOR_SCAN_LIMIT", raising=False)
    else:
        monkeypatch.setenv("LORE_MEMORY_VECTOR_SCAN_LIMIT", raw)
    assert vector_scan_limit() == expected
