"""OpenRouter chat-completions provider (OpenAI-compatible API over urllib).

Default model: ``nvidia/nemotron-3-ultra-550b-a55b:free`` (override with
``LORE_OPENROUTER_MODEL``). A faster free alternative is
``nvidia/nemotron-3.5-lightning:free`` — set it via config or env if you want it.
"""

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..utils.paths import lore_home, restrict_file, secure_dir
from .base import LLMConfig, LLMProvider, LLMResponse
from .gate import ensure_cloud_allowed

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
ALLOW_PAID_ENV_VAR = "LORE_OPENROUTER_ALLOW_PAID"
_MAX_RETRIES = 2
_MAX_SLEEP_SECONDS = 30.0


class OpenRouterError(Exception):
    """OpenRouter API or response handling failure."""


def resolve_openrouter_model(config: LLMConfig) -> str:
    """Model id from config, then ``LORE_OPENROUTER_MODEL``, then default."""
    if config.model:
        return config.model
    return os.environ.get("LORE_OPENROUTER_MODEL") or DEFAULT_MODEL


def validate_openrouter_model(model: str) -> str:
    """Free-only guard: refuse model ids without the ``:free`` suffix.

    The shared OpenRouter account is not on the free tier, so an accidental
    non-free model id would cost real money. Applies to the default, to
    ``LORE_OPENROUTER_MODEL`` and to ``--model`` — they all arrive here through
    ``config.model``. Opt in with ``LORE_OPENROUTER_ALLOW_PAID=1``.

    The message names the model and the opt-in variable only; it never carries
    key material.
    """
    if model.endswith(":free") or os.environ.get(ALLOW_PAID_ENV_VAR) == "1":
        return model
    raise OpenRouterError(
        f"model {model!r} is not a free OpenRouter endpoint (':free' suffix required); "
        f"set {ALLOW_PAID_ENV_VAR}=1 to allow paid models"
    )


def _openrouter_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    normalized = host.lower().strip("[]")
    return normalized in ("localhost", "127.0.0.1", "::1")


def validate_openrouter_base_url(base_url: str) -> str:
    """Require https, or http only to a loopback host (local OpenAI-compatible servers)."""
    trimmed = base_url.rstrip("/")
    parsed = urlparse(trimmed)
    if not parsed.scheme or not parsed.netloc:
        raise OpenRouterError(
            "OpenRouter base URL must be an absolute https URL "
            "(http:// is only allowed for loopback hosts)"
        )
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return trimmed
    if scheme == "http" and _openrouter_loopback_host(parsed.hostname):
        return trimmed
    raise OpenRouterError(
        "OpenRouter base URL must use https (http:// is only allowed for loopback hosts)"
    )


def resolve_openrouter_base_url(config: LLMConfig) -> str:
    raw = config.base_url or os.environ.get("LORE_OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
    return validate_openrouter_base_url(raw)


class OpenRouterProvider(LLMProvider):
    def __init__(self, config: LLMConfig, allow_cloud: bool = False):
        super().__init__(config)
        # Transport-boundary opt-in: a directly constructed provider must refuse
        # to send without it just like one built by the factory (#PRIVACY.md).
        self.allow_cloud = allow_cloud
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            self._cache_dir = secure_dir(self._cache_dir_path)
        return self._cache_dir

    def _api_key(self) -> Optional[str]:
        return self.config.api_key or os.environ.get("OPENROUTER_API_KEY")

    def is_available(self) -> bool:
        return bool(self._api_key())

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        ensure_cloud_allowed("openrouter", self.allow_cloud)
        eff_max_tokens = max_tokens or self.config.max_tokens
        eff_temperature = temperature if temperature is not None else self.config.temperature
        base_url = resolve_openrouter_base_url(self.config)
        model = resolve_openrouter_model(self.config)
        validate_openrouter_model(model)  # refuse paid ids before cache lookup or request

        cache_key = None
        if self.config.cache_enabled:
            cache_key = self._get_cache_key(
                prompt,
                system_prompt,
                eff_max_tokens,
                eff_temperature,
                base_url,
            )
            cache_identity = self.cache_identity(prompt, system_prompt)
            cached = self._load_from_cache(cache_key)
            if cached:
                return cached

        model = resolve_openrouter_model(self.config)
        payload = self._chat_completion(
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=eff_max_tokens,
            temperature=eff_temperature,
        )

        content = payload["content"]
        tokens_used = payload.get("tokens_used", 0)
        cost = payload.get("cost")
        if cost:  # a ':free' model always reports 0 — anything else is real money
            print(
                f"warning: OpenRouter reported a cost of {cost} for model {model}",
                file=sys.stderr,
            )

        result = LLMResponse(
            content=content,
            model=model,
            tokens_used=tokens_used,
            cached=False,
        )

        if self.config.cache_enabled and cache_key:
            self._save_to_cache(cache_key, result, cache_identity)

        return result

    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt) for p in prompts]

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "provider": "openrouter",
            "model": resolve_openrouter_model(self.config),
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

    def _chat_completion(
        self,
        model: str,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        api_key = self._api_key()
        if not api_key:
            raise OpenRouterError("OpenRouter API key not configured")

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode("utf-8")

        url = f"{resolve_openrouter_base_url(self.config)}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.config.timeout) as response:
                    raw = response.read().decode("utf-8")
                return self._parse_completion_json(raw)
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise OpenRouterError(
                        f"OpenRouter authentication failed (HTTP {exc.code})"
                    ) from exc
                if exc.code == 429 or exc.code >= 500:
                    last_error = exc
                    if attempt < _MAX_RETRIES:
                        self._sleep_before_retry(attempt, exc.headers)
                        continue
                    raise OpenRouterError(
                        f"OpenRouter request failed after retries (HTTP {exc.code})"
                    ) from exc
                raise OpenRouterError(f"OpenRouter request failed (HTTP {exc.code})") from exc
            except (URLError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    self._sleep_before_retry(attempt, {})
                    continue
                raise OpenRouterError("OpenRouter request failed after retries") from exc

        raise OpenRouterError("OpenRouter request failed after retries") from last_error

    def _sleep_before_retry(self, attempt: int, headers: Any) -> None:
        delay = self._retry_delay_seconds(attempt, headers)
        time.sleep(delay)

    @staticmethod
    def _retry_delay_seconds(attempt: int, headers: Any) -> float:
        retry_after: Optional[str] = None
        if headers is not None:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_SLEEP_SECONDS)
            except ValueError:
                pass
        return min(2.0**attempt, _MAX_SLEEP_SECONDS)

    @staticmethod
    def _parse_completion_json(raw: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter returned invalid JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterError("OpenRouter returned no choices")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            raise OpenRouterError("OpenRouter returned empty message content")

        usage = data.get("usage") or {}
        tokens_used = int(usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            cost = data.get("cost")

        return {"content": content, "tokens_used": tokens_used, "cost": cost}

    def _get_cache_key(
        self,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: int,
        temperature: float,
        base_url: str,
    ) -> str:
        """Base identity plus the *effective* request values chosen at call time.

        OpenRouter resolves the model and the base URL per request (fallbacks,
        proxy overrides), so the key has to use those resolved values rather than
        the configured ones (#186).
        """
        import json

        payload = self.cache_identity(prompt, system_prompt)
        payload["model"] = resolve_openrouter_model(self.config)
        payload["params"] = {
            **payload["params"],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "base_url": base_url,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _load_from_cache(self, cache_key: str) -> Optional[LLMResponse]:
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return None
        cache_file = cache_root / f"{cache_key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                return LLMResponse(
                    content=data["content"],
                    model=data["model"],
                    tokens_used=data.get("tokens_used", 0),
                    cached=True,
                )
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                logger.debug("Ignoring unreadable OpenRouter cache entry: %s", exc)
        return None

    def _save_to_cache(self, cache_key: str, response: LLMResponse, identity=None) -> None:
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return
        cache_file = cache_root / f"{cache_key}.json"
        try:
            with open(cache_file, "w") as f:
                json.dump(
                    {
                        "content": response.content,
                        "model": response.model,
                        "tokens_used": response.tokens_used,
                        "request": identity or {},
                    },
                    f,
                )
            restrict_file(cache_file)
        except OSError as exc:
            logger.debug("Could not write OpenRouter cache entry: %s", exc)
