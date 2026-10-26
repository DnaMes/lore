"""TRUSTED_PROXY is a boolean: only 1/true/yes/on (any case) enable proxy trust.

Every reader goes through ``web_utils.trusted_proxy_enabled``. Anything else —
unset, empty, ``0``, ``false``, ``no``, ``off`` or an unknown word — leaves the
forwarded headers ignored. Behaviour change: before this, any non-empty value
(including ``0``) enabled trust.
"""

from __future__ import annotations

import pytest

from lore.interfaces import web, web_utils


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    monkeypatch.delenv("LORE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("LORE_WEB_HOST", raising=False)
    monkeypatch.setattr(web, "_BIND_HOST", "")
    web.app.config["TESTING"] = True
    return web.app.test_client()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", " 1 "])
def test_true_values_enable_trust(monkeypatch, value):
    monkeypatch.setenv("TRUSTED_PROXY", value)
    assert web_utils.trusted_proxy_enabled() is True


@pytest.mark.parametrize(
    "value", ["0", "false", "FALSE", "no", "off", "", "  ", "2", "enabled", "y", "t"]
)
def test_other_values_leave_trust_off(monkeypatch, value):
    monkeypatch.setenv("TRUSTED_PROXY", value)
    assert web_utils.trusted_proxy_enabled() is False


def test_unset_leaves_trust_off(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    assert web_utils.trusted_proxy_enabled() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_false_value_ignores_forwarded_headers(client, monkeypatch, value):
    """A disabled flag never parses X-Forwarded-*, so a malformed one is no 400."""
    monkeypatch.setenv("TRUSTED_PROXY", value)
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "X-Forwarded-Host": "bad host value!!"},
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("value", ["1", "true", "on"])
def test_true_value_validates_forwarded_headers(client, monkeypatch, value):
    monkeypatch.setenv("TRUSTED_PROXY", value)
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "localhost:5000", "X-Forwarded-Host": "bad host value!!"},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_zero_does_not_trust_public_origins(client, monkeypatch, value):
    """``TRUSTED_PROXY=0`` used to enable LORE_PUBLIC_ORIGINS trust; now it must not."""
    monkeypatch.setenv("TRUSTED_PROXY", value)
    monkeypatch.setenv("LORE_PUBLIC_ORIGINS", "https://lore.example")
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "lore.example", "Origin": "https://lore.example"},
    )
    assert resp.status_code == 403


def test_true_trusts_public_origins(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY", "1")
    monkeypatch.setenv("LORE_PUBLIC_ORIGINS", "https://lore.example")
    resp = client.post(
        "/api/cache/clear",
        headers={"Host": "lore.example", "Origin": "https://lore.example"},
    )
    assert resp.status_code == 200
