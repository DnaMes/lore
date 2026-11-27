"""Generic OpenAI-compatible chat-completions provider over urllib."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..utils.paths import lore_home, restrict_file, secure_dir
from .base import LLMConfig, LLMProvider, LLMResponse
from .cache_io import sweep_temp_files
from .gate import ensure_cloud_allowed

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_MAX_SLEEP_SECONDS = 30.0


class OpenAICompatError(Exception):
    """OpenAI-compatible API or configuration failure."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse HTTP redirects so bearer tokens and prompts are never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)


def is_loopback_host(host: Optional[str]) -> bool:
    """True when ``host`` is ``localhost`` or a loopback IP address literal."""
    if not host:
        return False
    normalized = host.lower().strip("[]").rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_private_or_loopback_host(host: Optional[str]) -> bool:
    """True when ``host`` is loopback or a private-network IP address literal."""
    if is_loopback_host(host):
        return True
    if not host:
        return False
    normalized = host.lower().strip("[]").rstrip(".")
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def validate_openai_base_url(
    base_url: str,
    *,
    allow_private: bool = False,
    error_cls: Type[Exception] = OpenAICompatError,
    subject: str = "OpenAI-compatible base URL",
    include_private_hint: bool = False,
) -> str:
    """Require https, or http only for loopback (and optionally private-IP) hosts."""
    trimmed = base_url.strip().rstrip("/")
    parsed = urlparse(trimmed)
    hint = "loopback or private-network hosts" if include_private_hint else "loopback hosts"
    if not parsed.scheme or not parsed.netloc:
        raise error_cls(
            f"{subject} must be an absolute https URL (http:// is only allowed for {hint})"
        )
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return trimmed
    if scheme == "http":
        allowed = (
            is_private_or_loopback_host(parsed.hostname)
            if allow_private
            else is_loopback_host(parsed.hostname)
        )
        if allowed:
            return trimmed
    raise error_cls(f"{subject} must use https (http:// is only allowed for {hint})")


def resolve_openai_compat_base_url(config: LLMConfig) -> str:
    """Resolve and validate the base URL from config or ``LORE_LLM_BASE_URL``."""
    raw = (config.base_url or "").strip() or os.environ.get("LORE_LLM_BASE_URL", "").strip()
    if not raw:
        raise OpenAICompatError(
            "OpenAI-compatible base URL not configured: set LORE_LLM_BASE_URL or pass base_url"
        )
    return validate_openai_base_url(
        raw,
        allow_private=True,
        error_cls=OpenAICompatError,
        subject="OpenAI-compatible base URL",
        include_private_hint=True,
    )


def resolve_openai_compat_model(config: LLMConfig) -> str:
    """Resolve the model id from config or ``LORE_LLM_MODEL``."""
    model = (config.model or "").strip() or os.environ.get("LORE_LLM_MODEL", "").strip()
    if not model:
        raise OpenAICompatError(
            "OpenAI-compatible model not configured: set LORE_LLM_MODEL or pass --model"
        )
    return model


def resolve_openai_compat_api_key(config: LLMConfig) -> Optional[str]:
    """Resolve optional API key from config, ``LORE_LLM_API_KEY``, or ``LORE_LLM_API_KEY_ENV``."""
    direct = (config.api_key or "").strip()
    if direct:
        return direct
    env_key = os.environ.get("LORE_LLM_API_KEY", "").strip()
    if env_key:
        return env_key
    key_env_name = os.environ.get("LORE_LLM_API_KEY_ENV", "").strip()
    if key_env_name:
        named_val = os.environ.get(key_env_name, "").strip()
        if named_val:
            return named_val
    return None


class OpenAICompatProvider(LLMProvider):
    """Chat-completions client for any OpenAI-compatible HTTP endpoint."""

    def __init__(self, config: LLMConfig, allow_cloud: bool = False):
        super().__init__(config)
        self.allow_cloud = allow_cloud
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            cache_dir = secure_dir(self._cache_dir_path)
            sweep_temp_files(cache_dir)
            self._cache_dir = cache_dir
        return self._cache_dir

    def is_available(self) -> bool:
        try:
            resolve_openai_compat_base_url(self.config)
            resolve_openai_compat_model(self.config)
            return True
        except OpenAICompatError as exc:
            logger.debug("OpenAI-compatible provider unavailable: %s", exc)
            return False

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        ensure_cloud_allowed("openai-compatible", self.allow_cloud)
        eff_max_tokens = max_tokens or self.config.max_tokens
        eff_temperature = temperature if temperature is not None else self.config.temperature
        base_url = resolve_openai_compat_base_url(self.config)
        model = resolve_openai_compat_model(self.config)

        cache_key = None
        cache_identity = None
        if self.config.cache_enabled:
            cache_identity = self._resolved_cache_identity(
                prompt,
                system_prompt,
                eff_max_tokens,
                eff_temperature,
                model,
            )
            cache_key = self._hash_cache_identity(cache_identity)
            cached = self._load_from_cache(cache_key)
            if cached:
                return cached

        payload = self._chat_completion(
            base_url=base_url,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=eff_max_tokens,
            temperature=eff_temperature,
        )

        result = LLMResponse(
            content=payload["content"],
            model=model,
            tokens_used=payload.get("tokens_used", 0),
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
        try:
            model = resolve_openai_compat_model(self.config)
        except OpenAICompatError:
            model = ""
        return {
            "provider": "openai-compatible",
            "model": model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

    def _chat_completion(
        self,
        *,
        base_url: str,
        model: str,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
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

        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = resolve_openai_compat_api_key(self.config)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        for attempt in range(_MAX_RETRIES + 1):
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                with _NO_REDIRECT_OPENER.open(request, timeout=self.config.timeout) as response:
                    raw = response.read().decode("utf-8")
                return self._parse_completion_json(raw)
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise OpenAICompatError(
                        f"OpenAI-compatible authentication failed (HTTP {exc.code})"
                    ) from None
                if exc.code == 429 or exc.code >= 500:
                    if attempt < _MAX_RETRIES:
                        self._sleep_before_retry(attempt, exc.headers)
                        continue
                    raise OpenAICompatError(
                        f"OpenAI-compatible request failed after retries (HTTP {exc.code})"
                    ) from None
                raise OpenAICompatError(
                    f"OpenAI-compatible request failed (HTTP {exc.code})"
                ) from None
            except (URLError, OSError, TimeoutError):
                if attempt < _MAX_RETRIES:
                    self._sleep_before_retry(attempt, {})
                    continue
                raise OpenAICompatError("OpenAI-compatible request failed after retries") from None

        raise OpenAICompatError("OpenAI-compatible request failed after retries")

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
        except json.JSONDecodeError:
            raise OpenAICompatError("OpenAI-compatible endpoint returned invalid JSON") from None

        choices = data.get("choices") or []
        if not choices:
            raise OpenAICompatError("OpenAI-compatible endpoint returned no choices")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            raise OpenAICompatError("OpenAI-compatible endpoint returned empty message content")

        usage = data.get("usage") or {}
        tokens_used = int(usage.get("total_tokens") or 0)
        return {"content": content, "tokens_used": tokens_used}

    def _resolved_cache_identity(
        self,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: int,
        temperature: float,
        model: str,
    ) -> Dict[str, Any]:
        """Cache identity with resolved sampling params and model, never base_url."""
        payload = self.cache_identity(prompt, system_prompt)
        payload["model"] = model
        payload["params"] = {
            **payload["params"],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "base_url": "",
        }
        return payload

    @staticmethod
    def _hash_cache_identity(payload: Dict[str, Any]) -> str:
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
                logger.debug("Ignoring unreadable OpenAI-compatible cache entry: %s", exc)
        return None

    def _save_to_cache(self, cache_key: str, response: LLMResponse, identity=None) -> None:
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return
        cache_file = cache_root / f"{cache_key}.json"
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=cache_root, prefix=f".{cache_key}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "content": response.content,
                        "model": response.model,
                        "tokens_used": response.tokens_used,
                        "request": identity or {},
                    },
                    f,
                )
            os.replace(tmp_path, cache_file)
            tmp_path = None
            restrict_file(cache_file)
        except OSError as exc:
            logger.debug("Could not write OpenAI-compatible cache entry: %s", exc)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
