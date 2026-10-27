"""OpenRouter LLM provider (no network)."""

from __future__ import annotations

import io
import json
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from lore.llm.base import LLMConfig
from lore.llm.factory import get_provider
from lore.llm.openrouter import DEFAULT_MODEL, OpenRouterError, OpenRouterProvider
from lore.services.summarize_cli import default_model, provider_locality


@pytest.fixture(autouse=True)
def _allow_cloud_direct_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests build OpenRouterProvider directly; opt in at the transport gate.

    The factory/CLI gates are tested elsewhere (test_llm_cloud_optin.py,
    test_cli_openrouter_optin.py); here only the provider itself is under test.
    """
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")


def _completion_body(content: str = "hello", tokens: int = 42) -> bytes:
    payload = {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    }
    return json.dumps(payload).encode("utf-8")


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: Optional[int] = None):
        calls.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return handler(calls)

    monkeypatch.setattr("lore.llm.openrouter.urlopen", fake_urlopen)
    return calls


@pytest.fixture
def lore_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = "or-test-key-" + "x" * 16
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.delenv("LORE_OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("LORE_OPENROUTER_MODEL", raising=False)
    return key


def test_request_shape_and_authorization(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("ok", 10))

    calls = _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="test/model:free", cache_enabled=False)
    )
    resp = provider.generate("user prompt", system_prompt="sys", max_tokens=100, temperature=0.2)

    assert resp.content == "ok"
    assert resp.tokens_used == 10
    assert len(calls) == 1
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {lore_key}"
    body = calls[0]["body"]
    assert body["model"] == "test/model:free"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.2
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user prompt"},
    ]


def test_content_parsing(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("parsed text", 7))

    _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    resp = provider.generate("p")
    assert resp.model == "m:free"
    assert resp.content == "parsed text"
    assert resp.tokens_used == 7


def test_429_then_200_retries_with_sleep(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    sleeps: List[float] = []
    monkeypatch.setattr("lore.llm.openrouter.time.sleep", lambda s: sleeps.append(s))

    attempt = 0

    def handler(_calls: List[Dict[str, Any]]):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "rate limit",
                {"Retry-After": "2"},
                io.BytesIO(b""),
            )
        return _FakeHTTPResponse(_completion_body("after retry"))

    _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    resp = provider.generate("p")
    assert resp.content == "after retry"
    assert sleeps == [2.0]


def test_401_raises_without_leaking_key(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        raise HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"invalid"}'),
        )

    _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    with pytest.raises(OpenRouterError) as exc_info:
        provider.generate("p")
    msg = str(exc_info.value)
    assert lore_key not in msg
    assert "401" in msg


def test_empty_content_raises(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("   "))

    _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    with pytest.raises(OpenRouterError, match="empty message content"):
        provider.generate("p")


def test_for_transcripts_disables_cache() -> None:
    cfg = LLMConfig.for_transcripts(provider="openrouter", cache_enabled=True)
    assert cfg.cache_enabled is False


def test_factory_returns_openrouter_provider(
    monkeypatch: pytest.MonkeyPatch, lore_key: str
) -> None:
    monkeypatch.delenv("LORE_OPENROUTER_MODEL", raising=False)
    provider = get_provider("openrouter", api_key=lore_key, allow_cloud=True)
    from lore.llm.openrouter import OpenRouterProvider as ORP

    assert isinstance(provider, ORP)
    assert provider.config.model == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_factory_unknown_provider_lists_openrouter() -> None:
    with pytest.raises(ValueError) as exc_info:
        get_provider("not-a-provider")
    assert "openrouter" in str(exc_info.value)


def test_provider_locality_openrouter_is_cloud() -> None:
    assert provider_locality("openrouter") == "cloud"


def test_default_model_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_OPENROUTER_MODEL", raising=False)
    assert default_model("openrouter") == DEFAULT_MODEL
    monkeypatch.setenv("LORE_OPENROUTER_MODEL", "custom/model")
    assert default_model("openrouter") == "custom/model"


def test_cache_miss_when_temperature_max_tokens_or_base_url_change(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, tmp_path: Path
) -> None:
    responses = iter(["first", "second", "third", "fourth"])

    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body(next(responses)))

    calls = _install_urlopen(monkeypatch, handler)
    cache_dir = str(tmp_path / "or-cache")
    base_cfg = dict(
        provider="openrouter",
        model="m:free",
        cache_enabled=True,
        cache_dir=cache_dir,
        temperature=0.5,
        max_tokens=50,
    )

    provider = OpenRouterProvider(LLMConfig(**base_cfg))
    assert provider.generate("same prompt").content == "first"
    assert len(calls) == 1

    provider = OpenRouterProvider(LLMConfig(**{**base_cfg, "temperature": 0.9}))
    assert provider.generate("same prompt").content == "second"
    assert len(calls) == 2

    provider = OpenRouterProvider(LLMConfig(**{**base_cfg, "max_tokens": 99}))
    assert provider.generate("same prompt").content == "third"
    assert len(calls) == 3

    provider = OpenRouterProvider(LLMConfig(**{**base_cfg, "base_url": "http://127.0.0.1:8080"}))
    assert provider.generate("same prompt").content == "fourth"
    assert len(calls) == 4
    assert calls[-1]["url"] == "http://127.0.0.1:8080/chat/completions"


def test_urlerror_then_200_retries(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    sleeps: List[float] = []
    monkeypatch.setattr("lore.llm.openrouter.time.sleep", lambda s: sleeps.append(s))
    attempt = 0

    def handler(_calls: List[Dict[str, Any]]):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise URLError("connection reset")
        return _FakeHTTPResponse(_completion_body("recovered"))

    calls = _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    resp = provider.generate("p")
    assert resp.content == "recovered"
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_persistent_urlerror_raises_after_retries(
    monkeypatch: pytest.MonkeyPatch, lore_key: str
) -> None:
    monkeypatch.setattr("lore.llm.openrouter.time.sleep", lambda _s: None)

    def handler(_calls: List[Dict[str, Any]]):
        raise URLError("down")

    calls = _install_urlopen(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    )
    with pytest.raises(OpenRouterError, match="after retries"):
        provider.generate("p")
    assert len(calls) == 3


def test_base_url_scheme_policy(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter",
            model="m:free",
            cache_enabled=False,
            base_url="http://example.com/v1",
        )
    )
    with pytest.raises(OpenRouterError, match="https"):
        provider.generate("p")

    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("ok"))

    calls = _install_urlopen(monkeypatch, handler)
    OpenRouterProvider(
        LLMConfig(
            provider="openrouter",
            model="m:free",
            cache_enabled=False,
            base_url="http://127.0.0.1:8080",
        )
    ).generate("p")
    assert calls[-1]["url"] == "http://127.0.0.1:8080/chat/completions"

    calls.clear()
    OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False)
    ).generate("p2")
    assert calls[-1]["url"] == "https://openrouter.ai/api/v1/chat/completions"


def test_openrouter_cache_request_metadata_uses_resolved_values(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, tmp_path: Path
) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("cached"))

    calls = _install_urlopen(monkeypatch, handler)
    cache_dir = tmp_path / "or-cache-resolved-meta"
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter",
            model="example/model:free",
            cache_enabled=True,
            cache_dir=str(cache_dir),
            max_tokens=4096,
            temperature=0.7,
        )
    )
    provider.generate("cache-me", max_tokens=100, temperature=0.1)
    assert len(calls) == 1
    assert calls[0]["body"]["model"] == "example/model:free"
    assert calls[0]["body"]["max_tokens"] == 100
    assert calls[0]["body"]["temperature"] == 0.1

    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    stored = json.loads(cache_files[0].read_text(encoding="utf-8"))
    request = stored["request"]
    assert request["model"] == "example/model:free"
    assert request["params"]["max_tokens"] == 100
    assert request["params"]["temperature"] == 0.1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_openrouter_cache_write_is_atomic_0600(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, tmp_path: Path
) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("cached-body"))

    _install_urlopen(monkeypatch, handler)
    cache_dir = tmp_path / "or-cache-atomic"
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter",
            model="m:free",
            cache_enabled=True,
            cache_dir=str(cache_dir),
        )
    )
    provider.generate("cache-me")
    tmp_left = list(cache_dir.glob(".*.tmp"))
    assert tmp_left == []
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    assert stat.S_IMODE(cache_files[0].stat().st_mode) == 0o600


def test_cache_file_created_with_mode_0600(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, tmp_path: Path
) -> None:
    def handler(_calls: List[Dict[str, Any]]):
        return _FakeHTTPResponse(_completion_body("cached-body"))

    _install_urlopen(monkeypatch, handler)
    cache_dir = tmp_path / "or-cache-modes"
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter",
            model="m:free",
            cache_enabled=True,
            cache_dir=str(cache_dir),
        )
    )
    provider.generate("cache-me")
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    mode = cache_files[0].stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


# --- free-only guard and cost reporting (Track L step 2b) ----------------


def test_free_model_is_accepted(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    monkeypatch.delenv("LORE_OPENROUTER_ALLOW_PAID", raising=False)
    calls = _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(_completion_body("ok")))
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter", model="nvidia/nemotron-3.5-lightning:free", cache_enabled=False
        )
    )
    assert provider.generate("hi").content == "ok"
    assert len(calls) == 1


@pytest.mark.parametrize("model", ["openai/gpt-5.6-luna", "nvidia/nemotron-3-ultra-550b-a55b"])
def test_paid_model_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, model: str
) -> None:
    monkeypatch.delenv("LORE_OPENROUTER_ALLOW_PAID", raising=False)
    calls = _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(_completion_body("x")))
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model=model, cache_enabled=False)
    )

    with pytest.raises(OpenRouterError) as exc:
        provider.generate("hi")

    assert calls == []  # refused before a request went out
    message = str(exc.value)
    assert model in message
    assert "LORE_OPENROUTER_ALLOW_PAID" in message
    assert lore_key not in message  # no key material in the error


def test_paid_model_refused_when_coming_from_env(
    monkeypatch: pytest.MonkeyPatch, lore_key: str
) -> None:
    monkeypatch.delenv("LORE_OPENROUTER_ALLOW_PAID", raising=False)
    monkeypatch.setenv("LORE_OPENROUTER_MODEL", "openai/gpt-5.6-luna")
    calls = _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(_completion_body("x")))

    with pytest.raises(OpenRouterError):
        get_provider("openrouter", cache_enabled=False, allow_cloud=True).generate("hi")

    assert calls == []


def test_allow_paid_env_opts_in(monkeypatch: pytest.MonkeyPatch, lore_key: str) -> None:
    monkeypatch.setenv("LORE_OPENROUTER_ALLOW_PAID", "1")
    calls = _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(_completion_body("paid")))
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="openai/gpt-5.6-luna", cache_enabled=False)
    )
    assert provider.generate("hi").content == "paid"
    assert len(calls) == 1


def test_zero_cost_is_silent(monkeypatch: pytest.MonkeyPatch, lore_key: str, capsys) -> None:
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 5, "cost": 0},
    }
    _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(json.dumps(body).encode()))
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter", model="nvidia/nemotron-3.5-lightning:free", cache_enabled=False
        )
    )
    provider.generate("hi")
    assert "cost" not in capsys.readouterr().err.lower()


def test_nonzero_cost_warns_on_stderr(
    monkeypatch: pytest.MonkeyPatch, lore_key: str, capsys
) -> None:
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 5, "cost": 0.0042},
    }
    _install_urlopen(monkeypatch, lambda _c: _FakeHTTPResponse(json.dumps(body).encode()))
    provider = OpenRouterProvider(
        LLMConfig(
            provider="openrouter", model="nvidia/nemotron-3.5-lightning:free", cache_enabled=False
        )
    )
    provider.generate("hi")
    err = capsys.readouterr().err
    assert "0.0042" in err
    assert "cost" in err.lower()
