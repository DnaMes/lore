"""Proxy and origin validation behind a reverse proxy (#193).

The browser's ``Origin`` must be accepted when it names an origin the operator
configured (``LORE_PUBLIC_ORIGINS``) *and* the existing trusted-proxy flag is
set; loopback origins keep working without any configuration; everything else
is rejected. While a proxy is supposedly in front, malformed ``X-Forwarded-*``
headers on state-changing routes are rejected too, because that is exactly the
path a spoofing client walks into.
"""

from __future__ import annotations

from typing import Dict

import pytest

from lore.interfaces import web, web_utils

POST = "/api/cache/clear"  # state-changing route guarded by _check_local_origin


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def _post(client, headers: Dict[str, str]):
    return client.post(POST, headers=headers)


def _trusted(monkeypatch: pytest.MonkeyPatch, origins: str) -> None:
    """Configure a proxied deployment: flag on, origins as given (``""`` = none)."""
    monkeypatch.setenv("TRUSTED_PROXY", "1")
    if origins:
        monkeypatch.setenv("LORE_PUBLIC_ORIGINS", origins)


# ---------------------------------------------------------------------------
# direct: no trusted proxy configured
# ---------------------------------------------------------------------------


def test_direct_loopback_origin_passes(client):
    assert _post(client, {"Origin": "http://localhost"}).status_code != 403
    # An Origin is only accepted when it exactly matches the request's own
    # scheme://host:port — send the matching Host header for :5000 (follow-up
    # to #193: cross-port loopback origins are no longer a free pass).
    assert (
        _post(client, {"Origin": "http://127.0.0.1:5000", "Host": "127.0.0.1:5000"}).status_code
        != 403
    )


def test_direct_foreign_origin_is_rejected(client):
    assert _post(client, {"Origin": "https://evil.example"}).status_code == 403


def test_configured_origins_need_the_trusted_proxy_flag(client, monkeypatch):
    """`LORE_PUBLIC_ORIGINS` alone changes nothing — the proxy flag gates it (#193)."""
    monkeypatch.setenv("LORE_PUBLIC_ORIGINS", "https://lore.example:8443")
    assert _post(client, {"Origin": "https://lore.example:8443"}).status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "https://",
        "javascript:alert(1)",
        "https://evil.example/path",
        "https://user@evil.example",
        "https://evil.example:99999",
        "not an origin",
    ],
)
def test_malformed_origin_is_rejected(client, origin):
    assert _post(client, {"Origin": origin}).status_code == 403


# ---------------------------------------------------------------------------
# behind the trusted proxy
# ---------------------------------------------------------------------------


def test_trusted_configured_origin_with_tls_and_non_default_port(client, monkeypatch):
    _trusted(monkeypatch, "https://lore.example:8443, http://10.0.0.5:5000")
    assert _post(client, {"Origin": "https://lore.example:8443"}).status_code != 403
    assert _post(client, {"Origin": "http://10.0.0.5:5000"}).status_code != 403
    assert _post(client, {"Origin": "http://localhost"}).status_code != 403


def test_trusted_wrong_scheme_or_port_is_rejected(client, monkeypatch):
    _trusted(monkeypatch, "https://lore.example:8443")
    # scheme and port are part of an origin — none of these is the configured one
    assert _post(client, {"Origin": "http://lore.example:8443"}).status_code == 403
    assert _post(client, {"Origin": "https://lore.example"}).status_code == 403
    assert _post(client, {"Origin": "https://lore.example:9443"}).status_code == 403


def test_trusted_unlisted_origin_is_rejected(client, monkeypatch):
    _trusted(monkeypatch, "https://lore.example")
    assert _post(client, {"Origin": "https://other.example"}).status_code == 403


def test_malformed_configured_entry_is_ignored(client, monkeypatch):
    _trusted(monkeypatch, "not-a-url, https://lore.example")
    assert _post(client, {"Origin": "not-a-url"}).status_code == 403
    assert _post(client, {"Origin": "https://lore.example"}).status_code != 403


def test_trusted_proxy_without_config_still_rejects_foreign_origin(client, monkeypatch):
    _trusted(monkeypatch, "")
    assert _post(client, {"Origin": "https://lore.example"}).status_code == 403


# ---------------------------------------------------------------------------
# X-Forwarded-* while a proxy is supposedly in front
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        {"X-Forwarded-Proto": "javascript"},
        {"X-Forwarded-Proto": "https, ftp"},
        {"X-Forwarded-Host": "evil..com"},
        {"X-Forwarded-Host": "lore.example:8443:99"},
        {"X-Forwarded-Host": "lore.example/x"},
        {"X-Forwarded-For": "not-an-ip"},
        {"X-Forwarded-For": "1.2.3.4,, 5.6.7.8"},
    ],
)
def test_trusted_proxy_rejects_malformed_forwarded_headers(client, monkeypatch, header):
    _trusted(monkeypatch, "https://lore.example")
    resp = _post(client, {"Origin": "https://lore.example", **header})
    assert resp.status_code == 400


def test_trusted_proxy_accepts_well_formed_forwarded_headers(client, monkeypatch):
    _trusted(monkeypatch, "https://lore.example")
    resp = _post(
        client,
        {
            "Origin": "https://lore.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "lore.example",
            "X-Forwarded-For": "203.0.113.7, 10.0.0.1",
        },
    )
    assert resp.status_code == 200


def test_malformed_forwarded_headers_are_rejected_even_without_an_origin(client, monkeypatch):
    _trusted(monkeypatch, "https://lore.example")
    assert _post(client, {"X-Forwarded-For": "nope"}).status_code == 400


def test_forwarded_headers_are_ignored_without_the_flag(client):
    """Untrusted client: the headers are never parsed, so garbage is irrelevant (#193)."""
    resp = _post(client, {"Origin": "http://localhost", "X-Forwarded-Proto": "javascript"})
    assert resp.status_code != 400
    assert resp.status_code != 403


def test_client_ip_ignores_forwarded_headers_without_the_flag(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    with web.app.test_request_context(
        "/", headers={"X-Forwarded-For": "9.9.9.9"}, environ_base={"REMOTE_ADDR": "127.0.0.1"}
    ):
        assert web_utils._client_ip() == "127.0.0.1"
    monkeypatch.setenv("TRUSTED_PROXY", "1")
    with web.app.test_request_context(
        "/", headers={"X-Forwarded-For": "9.9.9.9"}, environ_base={"REMOTE_ADDR": "127.0.0.1"}
    ):
        assert web_utils._client_ip() == "9.9.9.9"
