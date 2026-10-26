"""Host-header allowlist and mutation-Origin policy for the web UI (P1).

A request naming a foreign ``Host`` (DNS rebinding, poisoned Host) must be
rejected before any route runs; the defaults are the loopback names, the bind
host, and hosts derived from ``LORE_PUBLIC_ORIGINS`` / ``LORE_ALLOWED_HOSTS``,
whose literal ``*`` disables the check with one warning. Mutating requests
carrying an ``Origin`` must be this app's own scheme://host[:port] or a
configured public origin exactly — a loopback origin on a different port is
cross-origin like any other site. A missing ``Origin`` keeps the historical
behaviour (curl and MCP clients never send one).
"""

from __future__ import annotations

import logging

import pytest

from lore.cli import web as web_cli
from lore.interfaces import web, web_utils


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    monkeypatch.delenv("LORE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("LORE_WEB_HOST", raising=False)
    monkeypatch.setattr(web, "_BIND_HOST", "")
    web.app.config["TESTING"] = True
    return web.app.test_client()


# ---------------------------------------------------------------------------
# Host allowlist
# ---------------------------------------------------------------------------


def test_foreign_host_is_rejected_with_400(client):
    resp = client.get("/api/v1/sessions", headers={"Host": "attacker.example:5000"})
    assert resp.status_code == 400
    assert b"LORE_ALLOWED_HOSTS" in resp.data


@pytest.mark.parametrize(
    "host",
    ["localhost:5000", "127.0.0.1:5000", "[::1]:5000", "LOCALHOST:5000", "localhost"],
)
def test_default_hosts_are_allowed(client, host):
    resp = client.get("/api/v1/sessions", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("path", ["/api/health", "/api/ready"])
def test_health_and_ready_stay_reachable(client, host, path):
    resp = client.get(path, headers={"Host": host})
    assert resp.status_code == 200


def test_allowed_hosts_env_is_read_per_request(client, monkeypatch):
    """Config is read on every request, not frozen at app creation."""
    assert client.get("/api/health", headers={"Host": "lore.example.com"}).status_code == 400
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", " lore.example.com , other.example ")
    assert client.get("/api/health", headers={"Host": "lore.example.com"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "third.example"}).status_code == 400


def test_wildcard_allows_any_host_and_warns_once(client, monkeypatch, caplog):
    monkeypatch.setattr(web_utils, "_WILDCARD_HOSTS_WARNED", False)
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "*")
    caplog.set_level(logging.WARNING, logger="lore.interfaces.web_utils")
    assert client.get("/api/health", headers={"Host": "attacker.example:5000"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 200
    warned = [r for r in caplog.records if "LORE_ALLOWED_HOSTS" in r.getMessage()]
    assert len(warned) == 1


def test_public_origin_host_counts_as_allowed(client, monkeypatch):
    monkeypatch.setenv("LORE_PUBLIC_ORIGINS", "https://lore.example:8443")
    assert client.get("/api/health", headers={"Host": "lore.example:8443"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 400


def test_bind_host_counts_as_allowed(client, monkeypatch):
    monkeypatch.setattr(web, "_BIND_HOST", "10.1.2.3")
    assert client.get("/api/health", headers={"Host": "10.1.2.3"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "10.1.2.4"}).status_code == 400


# ---------------------------------------------------------------------------
# Mutation-Origin policy
# ---------------------------------------------------------------------------


def test_mutation_with_foreign_loopback_origin_is_rejected(client):
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "Origin": "http://localhost:9999"},
    )
    assert resp.status_code == 403


def test_same_origin_mutation_is_allowed(client):
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200


def test_mutation_without_origin_keeps_working(client):
    resp = client.post("/api/cache/clear", headers={"Host": "localhost:5000"})
    assert resp.status_code == 200


def test_get_with_foreign_origin_is_not_blocked(client):
    resp = client.get(
        "/api/v1/sessions",
        headers={"Host": "localhost:5000", "Origin": "http://localhost:9999"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Non-loopback bind warning (lore-web has no authentication)
# ---------------------------------------------------------------------------


def test_cli_warns_on_non_loopback_bind_without_hardening(capsys, monkeypatch):
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    monkeypatch.delenv("LORE_ALLOWED_HOSTS", raising=False)
    web_cli.warn_if_exposed_without_origin_config("0.0.0.0")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "LORE_ALLOWED_HOSTS" in err
    assert "authentication" in err


def test_cli_stays_quiet_with_hardening_or_loopback(capsys, monkeypatch):
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "lore.example.com")
    web_cli.warn_if_exposed_without_origin_config("0.0.0.0")
    monkeypatch.delenv("LORE_ALLOWED_HOSTS")
    monkeypatch.setenv("LORE_PUBLIC_ORIGINS", "https://lore.example")
    web_cli.warn_if_exposed_without_origin_config("0.0.0.0")
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS")
    web_cli.warn_if_exposed_without_origin_config("127.0.0.1")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_cli_warns_at_startup_via_main(capsys, monkeypatch):
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    monkeypatch.delenv("LORE_ALLOWED_HOSTS", raising=False)
    started: dict[str, object] = {}
    monkeypatch.setattr(web, "start_web_ui", lambda **kwargs: started.update(kwargs))
    assert web_cli.main(["--host", "0.0.0.0", "--port", "5098"]) == 0
    assert started == {"port": 5098, "host": "0.0.0.0", "debug": False}
    assert "LORE_ALLOWED_HOSTS" in capsys.readouterr().err
