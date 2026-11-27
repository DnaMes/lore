"""Tests for the generic OpenAI-compatible provider and related choices/extras."""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lore.llm.base import LLMConfig
from lore.llm.openai_compat import OpenAICompatError, OpenAICompatProvider
from lore.services.summarize_cli import (
    PROVIDER_CHOICES,
    provider_locality,
    web_summarize_enabled,
)


def _start_server(
    *,
    status_code: int = 200,
    response_body: Dict[str, Any] | None = None,
):
    recorded: List[Dict[str, Any]] = []
    payload = response_body or {
        "choices": [{"message": {"content": "ok summary"}}],
        "usage": {"total_tokens": 12},
    }

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length else ""
            recorded.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw_body) if raw_body else {},
                }
            )
            body_bytes = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, recorded


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in (
        "LORE_LLM_BASE_URL",
        "LORE_LLM_MODEL",
        "LORE_LLM_API_KEY",
        "LORE_LLM_API_KEY_ENV",
        "LORE_SUMMARY_PROVIDER",
        "LORE_ALLOW_CLOUD",
    ):
        monkeypatch.delenv(var, raising=False)


def test_request_uses_direct_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    server, thread, recorded = _start_server()
    try:
        host, port = server.server_address
        monkeypatch.setenv("LORE_LLM_BASE_URL", f"http://{host}:{port}/v1")
        monkeypatch.setenv("LORE_LLM_MODEL", "local-test-model")
        monkeypatch.setenv("LORE_LLM_API_KEY", "secret-direct-key")

        provider = OpenAICompatProvider(
            LLMConfig(provider="openai-compatible", model="", cache_enabled=False)
        )
        res = provider.generate("hello")

        assert res.content == "ok summary"
        assert res.tokens_used == 12
        assert len(recorded) == 1
        assert recorded[0]["path"] == "/v1/chat/completions"
        assert recorded[0]["headers"].get("authorization") == "Bearer secret-direct-key"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_request_uses_named_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    server, thread, recorded = _start_server()
    try:
        host, port = server.server_address
        monkeypatch.setenv("LORE_LLM_BASE_URL", f"http://{host}:{port}/v1")
        monkeypatch.setenv("LORE_LLM_MODEL", "local-test-model")
        monkeypatch.setenv("LORE_LLM_API_KEY_ENV", "CUSTOM_PROVIDER_TOKEN")
        monkeypatch.setenv("CUSTOM_PROVIDER_TOKEN", "secret-named-key")

        provider = OpenAICompatProvider(
            LLMConfig(provider="openai-compatible", model="", cache_enabled=False)
        )
        res = provider.generate("hello")

        assert res.content == "ok summary"
        assert recorded[0]["path"] == "/v1/chat/completions"
        assert recorded[0]["headers"].get("authorization") == "Bearer secret-named-key"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_request_omits_authorization_header_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, recorded = _start_server()
    try:
        host, port = server.server_address
        monkeypatch.setenv("LORE_LLM_BASE_URL", f"http://{host}:{port}/v1")
        monkeypatch.setenv("LORE_LLM_MODEL", "local-test-model")

        provider = OpenAICompatProvider(
            LLMConfig(provider="openai-compatible", model="", cache_enabled=False)
        )
        provider.generate("hello")

        assert recorded[0]["path"] == "/v1/chat/completions"
        assert "authorization" not in recorded[0]["headers"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_non_loopback_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_LLM_BASE_URL", "http://example.com/v1")
    monkeypatch.setenv("LORE_LLM_MODEL", "m")
    provider = OpenAICompatProvider(
        LLMConfig(provider="openai-compatible", model="", cache_enabled=False),
        allow_cloud=True,
    )
    with pytest.raises(OpenAICompatError, match="https"):
        provider.generate("hello")


def test_missing_base_url_or_model_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LORE_LLM_MODEL", "m")
    provider_no_url = OpenAICompatProvider(
        LLMConfig(provider="openai-compatible", model="", cache_enabled=False),
        allow_cloud=True,
    )
    with pytest.raises(OpenAICompatError, match="LORE_LLM_BASE_URL"):
        provider_no_url.generate("hello")

    monkeypatch.setenv("LORE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.delenv("LORE_LLM_MODEL", raising=False)
    provider_no_model = OpenAICompatProvider(
        LLMConfig(provider="openai-compatible", model="", cache_enabled=False),
        allow_cloud=True,
    )
    with pytest.raises(OpenAICompatError, match="LORE_LLM_MODEL"):
        provider_no_model.generate("hello")


def test_provider_locality_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert provider_locality("openai-compatible") == "local"

    monkeypatch.setenv("LORE_LLM_BASE_URL", "http://localhost:11434/v1")
    assert provider_locality("openai-compatible") == "local"

    monkeypatch.setenv("LORE_LLM_BASE_URL", "https://api.example.com/v1")
    assert provider_locality("openai-compatible") == "cloud"


def test_web_summarize_enabled_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LORE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert web_summarize_enabled() is True

    monkeypatch.setenv("LORE_LLM_BASE_URL", "https://api.example.com/v1")
    assert web_summarize_enabled() is False


def test_api_key_never_leaks_on_401(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_key = "sk-super-secret-token-value-12345"
    server, thread, _recorded = _start_server(status_code=401, response_body={"error": "bad"})
    try:
        host, port = server.server_address
        monkeypatch.setenv("LORE_LLM_BASE_URL", f"http://{host}:{port}/v1")
        monkeypatch.setenv("LORE_LLM_MODEL", "local-test-model")
        monkeypatch.setenv("LORE_LLM_API_KEY", secret_key)

        provider = OpenAICompatProvider(
            LLMConfig(provider="openai-compatible", model="", cache_enabled=False)
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(OpenAICompatError) as exc_info:
                provider.generate("hello")

        assert "401" in str(exc_info.value)
        assert secret_key not in str(exc_info.value)
        assert secret_key not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_provider_choices_membership() -> None:
    for expected in ("openai-compatible", "claude-cli", "codex-cli", "gemini-cli"):
        assert expected in PROVIDER_CHOICES
    assert "antigravity-cli" not in PROVIDER_CHOICES


def test_gemini_lazy_import_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.generativeai", None)
    import importlib

    gemini_mod = importlib.import_module("lore.llm.gemini")
    provider = gemini_mod.GeminiProvider(
        LLMConfig(provider="gemini", model="gemini-2.0-flash", cache_enabled=False),
        allow_cloud=True,
    )
    with pytest.raises(ImportError, match=r"pip install google-generativeai"):
        provider._get_client()
