"""OpenRouter chat-completions provider (OpenAI-compatible API over urllib).

Default model: ``nvidia/nemotron-3-ultra-550b-a55b:free`` (override with
``LORE_OPENROUTER_MODEL``). A faster free alternative is
``nvidia/nemotron-3.5-lightning:free`` — set it via config or env if you want it.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..utils.paths import lore_home
from .base import LLMConfig, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_MAX_RETRIES = 2
_MAX_SLEEP_SECONDS = 30.0


class OpenRouterError(Exception):
    """OpenRouter API or response handling failure."""


def resolve_openrouter_model(config: LLMConfig) -> str:
    """Model id from config, then ``LORE_OPENROUTER_MODEL``, then default."""
    if config.model:
        return config.model
    return os.environ.get("LORE_OPENROUTER_MODEL") or DEFAULT_MODEL


def resolve_openrouter_base_url(config: LLMConfig) -> str:
    raw = config.base_url or os.environ.get("LORE_OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
    return raw.rstrip("/")


class OpenRouterProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            self._cache_dir = self._cache_dir_path
            self._cache_dir.mkdir(parents=True, exist_ok=True)
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
        cache_key = None
        if self.config.cache_enabled:
            cache_key = self._get_cache_key(prompt, system_prompt)
            cached = self._load_from_cache(cache_key)
            if cached:
                return cached

        model = resolve_openrouter_model(self.config)
        payload = self._chat_completion(
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens or self.config.max_tokens,
            temperature=temperature if temperature is not None else self.config.temperature,
        )

        content = payload["content"]
        tokens_used = payload.get("tokens_used", 0)

        result = LLMResponse(
            content=content,
            model=model,
            tokens_used=tokens_used,
            cached=False,
        )

        if self.config.cache_enabled and cache_key:
            self._save_to_cache(cache_key, result)

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
            except URLError as exc:
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

        return {"content": content, "tokens_used": tokens_used}

    def _get_cache_key(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        model = resolve_openrouter_model(self.config)
        combined = f"{system_prompt or ''}|{prompt}|{model}"
        return hashlib.sha256(combined.encode()).hexdigest()

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

    def _save_to_cache(self, cache_key: str, response: LLMResponse) -> None:
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
                    },
                    f,
                )
        except OSError as exc:
            logger.debug("Could not write OpenRouter cache entry: %s", exc)
