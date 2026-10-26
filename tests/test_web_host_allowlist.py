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


@pytest.mark.parametrize(
    "host",
    [
        "localhost:0",
        "localhost:65536",
        "localhost:" + ("9" * 4500),
        "[::1]:65536",
        "[::1]:0",
    ],
)
def test_invalid_host_ports_are_rejected_with_400(client, host):
    """Port 0 / out-of-range / absurd digit strings must be 400, never 500."""
    resp = client.get("/api/health", headers={"Host": host})
    assert resp.status_code == 400


def test_bracketed_ipv6_with_valid_port_is_allowed(client):
    assert client.get("/api/health", headers={"Host": "[::1]:5000"}).status_code == 200


def test_default_http_ports_match_portless(client):
    """``:80`` / ``:443`` normalise equal to a portless Host/Origin pair."""
    assert (
        web_utils.normalize_origin("http", "lore.example", 80)
        == web_utils.normalize_origin("http", "lore.example", None)
        == "http://lore.example"
    )
    assert (
        web_utils.normalize_origin("https", "lore.example", 443)
        == web_utils.normalize_origin("https", "lore.example", None)
        == "https://lore.example"
    )
    # Host allowlist: an entry without a port matches the default-port spelling.
    assert web_utils.host_header_allowed("localhost:80")
    assert web_utils.parse_origin("http://localhost:80") == ("http", "localhost", 80)
    assert web_utils.normalize_origin(*web_utils.parse_origin("http://localhost:80")) == (
        web_utils.normalize_origin("http", "localhost", None)
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost:" + ("9" * 4500),
        "http://[::1]:65536",
    ],
)
def test_invalid_origin_ports_are_rejected_with_403(client, origin):
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "Origin": origin},
    )
    assert resp.status_code == 403


# Non-ASCII digits (``str.isdigit`` accepts them, ``int`` does not) and other
# non-ASCII spellings used to reach ``int()`` and 500. They must be 400/403.
NON_ASCII_HOSTS = [
    "localhost:\u00b2",  # superscript two
    "localhost:\uff11\uff12",  # fullwidth 12
    "[::1]:\u00b2",
    "localhost:",  # empty port
    "lo\u043aalhost:5000",  # Cyrillic k: IDN look-alike
    "l\u00f6kal.example:5000",
]


@pytest.mark.parametrize("host", NON_ASCII_HOSTS)
def test_non_ascii_or_malformed_host_is_400_not_500(client, host):
    resp = client.get("/api/health", headers={"Host": host})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:\u00b2",
        "http://localhost:\uff11\uff12",
        "http://[::1]:\u00b2",
        "http://localhost:",  # empty port
        "http://l\u00f6kal:5000",
    ],
)
def test_non_ascii_or_malformed_origin_is_403_not_500(client, origin):
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "Origin": origin},
    )
    assert resp.status_code == 403


def test_fullwidth_port_in_origin_is_not_same_origin(client):
    """Fullwidth ``１２`` used to parse as port 12; it must not match anything."""
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:12", "Origin": "http://localhost:\uff11\uff12"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "value",
    ["\u00b2", "\uff11\uff12", "\u0661", "1\u00b2", "[::1]:\u00b2", "\x00", "\ud800", "["],
)
def test_parsers_never_raise_on_hostile_input(value):
    """Host/Origin helpers return None or False for malformed input, never raise."""
    assert web_utils.parse_origin(value) is None
    assert web_utils.parse_origin("http://" + value) is None
    assert web_utils.host_header_allowed(value) is False
    assert web_utils._split_host(value) is None


@pytest.mark.parametrize("value", ["12", "1", "65535"])
def test_ascii_port_bounds_are_accepted(value):
    assert web_utils._parse_port(value) == int(value)


@pytest.mark.parametrize("value", ["", "0", "65536", "\u00b2", "\uff11\uff12", "+1", " 1"])
def test_non_ascii_or_out_of_range_ports_are_none(value):
    assert web_utils._parse_port(value) is None


def test_forwarded_host_non_ascii_is_400_with_proxy(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY", "1")
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "X-Forwarded-Host": "localhost:\u00b2"},
    )
    assert resp.status_code == 400


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
