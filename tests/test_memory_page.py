"""Tests for the /memory web page (#33).

The page browses and searches the shared agent-memory store and lets a
memory be deleted.
"""

from __future__ import annotations

import pytest

from lore.interfaces import web, web_data
from lore.storage import add_memory


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Test client with the v2 store + INDEX_PATH pointed at a temp dir."""
    monkeypatch.setattr(web, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    with web.app.test_client() as c:
        yield c, tmp_path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_memory_page_renders_empty(client):
    c, _ = client
    resp = c.get("/memory")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No memory entries" in body


def test_memory_page_lists_entries(client):
    c, tmp_path = client
    add_memory(tmp_path, "decision", "Use Postgres 16", "Standardise on PG16.")
    add_memory(tmp_path, "fact", "API rate limit", "100 requests per minute.")
    body = c.get("/memory").get_data(as_text=True)
    assert "Use Postgres 16" in body
    assert "API rate limit" in body
    assert "2 entries" in body


def test_memory_page_kind_filter(client):
    c, tmp_path = client
    add_memory(tmp_path, "decision", "A decision entry", "body")
    add_memory(tmp_path, "fact", "A fact entry", "body")
    body = c.get("/memory?kind=fact").get_data(as_text=True)
    assert "A fact entry" in body
    assert "A decision entry" not in body


def test_memory_page_keyword_search(client, monkeypatch):
    import lore.storage.embeddings as emb

    # Keyword behaviour only: with the real model installed, hybrid recall would
    # also add semantically near memories (see test_memory_hybrid.py).
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)
    c, tmp_path = client
    add_memory(tmp_path, "fact", "Kubernetes notes", "ingress configuration")
    add_memory(tmp_path, "fact", "Database notes", "connection pooling")
    body = c.get("/memory?q=kubernetes").get_data(as_text=True)
    assert "Kubernetes notes" in body
    assert "Database notes" not in body


def test_memory_page_semantic_checkbox_is_true_or_auto(client, monkeypatch):
    seen = []

    def fake_recall(output_dir, query, **kwargs):
        seen.append(kwargs["semantic"])
        return []

    monkeypatch.setattr("lore.storage.recall_memory", fake_recall)
    c, _tmp = client
    c.get("/memory?q=x")
    c.get("/memory?q=x&semantic=1")
    assert seen == [None, True]


def test_memory_page_rejects_invalid_kind(client):
    c, _ = client
    assert c.get("/memory?kind=bogus").status_code == 400


def test_memory_page_rejects_invalid_query(client):
    c, _ = client
    assert c.get("/memory?q=" + "x" * 600).status_code == 400


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_memory_delete_removes_entry(client):
    c, tmp_path = client
    mid = add_memory(tmp_path, "note", "Throwaway memory", "delete me")
    resp = c.post(f"/memory/{mid}/delete", headers={"Origin": "http://localhost"})
    assert resp.status_code == 302
    # The entry no longer appears on the page.
    body = c.get("/memory").get_data(as_text=True)
    assert "Throwaway memory" not in body


def test_memory_delete_rejects_cross_origin(client):
    c, tmp_path = client
    mid = add_memory(tmp_path, "note", "Protected memory", "keep me")
    resp = c.post(f"/memory/{mid}/delete", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 403
    # The entry survives the rejected cross-origin delete.
    assert "Protected memory" in c.get("/memory").get_data(as_text=True)


# ---------------------------------------------------------------------------
# #43 — render safety: memory is agent-writable, must be HTML-escaped
# ---------------------------------------------------------------------------


def test_memory_html_in_body_is_escaped(client):
    c, tmp_path = client
    add_memory(
        tmp_path,
        "note",
        "XSS attempt title",
        "<script>alert('xss')</script> and <img src=x onerror=alert(1)>",
    )
    body = c.get("/memory").get_data(as_text=True)
    # The raw script/img tags must NOT appear executable — they're escaped.
    assert "<script>alert('xss')</script>" not in body
    assert "&lt;script&gt;" in body


def test_memory_html_in_title_is_escaped(client):
    c, tmp_path = client
    add_memory(tmp_path, "note", "<b>bold</b> injection title", "body")
    body = c.get("/memory").get_data(as_text=True)
    assert "<b>bold</b> injection title" not in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body


# ---------------------------------------------------------------------------
# #33 — source-session provenance shown on the memory page
# ---------------------------------------------------------------------------


def test_memory_page_shows_source_session(client):
    c, tmp_path = client
    add_memory(
        tmp_path,
        "decision",
        "Linked memory",
        "body",
        source_session="sess-provenance-xyz",
    )
    body = c.get("/memory").get_data(as_text=True)
    assert "From:" in body
    assert "sess-provenance-xyz"[:16] in body
    assert "/session/sess-provenance-xyz" in body


def test_memory_page_no_source_section_when_unlinked(client):
    c, tmp_path = client
    add_memory(tmp_path, "fact", "Unlinked memory", "body")
    body = c.get("/memory").get_data(as_text=True)
    assert "Unlinked memory" in body
    # No "From:" provenance line for a memory with no sources.
    assert "From:" not in body
