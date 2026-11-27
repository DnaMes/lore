"""/api/v1/search must honor and validate the scope parameter (#97).

Before the fix the route never read ``scope``: valid scopes were silently
ignored and garbage values returned 200 with unscoped results.
"""

from __future__ import annotations

import pytest

from annuli.interfaces import web


@pytest.fixture()
def client(monkeypatch):
    with web.app.test_client() as c:
        yield c


def _spy(captured):
    def fake_search_index(q, tool=None, project=None, limit=50, scope=None):
        captured.update(q=q, scope=scope)
        return []

    return fake_search_index


def test_scope_is_passed_through(client, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(web, "search_index", _spy(captured))
    r = client.get("/api/v1/search?q=docker&scope=user_only")
    assert r.status_code == 200
    assert captured["scope"] == "user_only"
    assert r.get_json()["scope"] == "user_only"


def test_scope_defaults_to_all(client, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(web, "search_index", _spy(captured))
    r = client.get("/api/v1/search?q=docker")
    assert r.status_code == 200
    assert captured["scope"] == "all"


def test_invalid_scope_returns_400(client, monkeypatch):
    called: dict = {}
    monkeypatch.setattr(web, "search_index", _spy(called))
    r = client.get("/api/v1/search?q=docker&scope=garbage")
    assert r.status_code == 400
    assert "scope" in r.get_json()["error"].lower()
    assert not called  # search never executed


def test_all_valid_scopes_accepted(client, monkeypatch):
    monkeypatch.setattr(web, "search_index", _spy({}))
    for scope in ("all", "user_only", "assistant_only", "tool_results"):
        r = client.get(f"/api/v1/search?q=docker&scope={scope}")
        assert r.status_code == 200, scope
