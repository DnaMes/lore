import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.paths import lore_home
from .base import LLMConfig, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

_NUM_CTX_MIN = 2048
_NUM_CTX_MAX = 131072
_NUM_CTX_DEFAULT = 8192


def resolve_ollama_num_ctx(config: LLMConfig) -> int:
    """Resolve Ollama context window size (config, then env, then default)."""
    if config.num_ctx is not None:
        value = config.num_ctx
    else:
        raw = os.environ.get("LORE_OLLAMA_NUM_CTX")
        if raw:
            try:
                value = int(raw.strip())
            except ValueError:
                value = _NUM_CTX_DEFAULT
        else:
            value = _NUM_CTX_DEFAULT
    return max(_NUM_CTX_MIN, min(_NUM_CTX_MAX, value))


class OllamaProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            self._cache_dir = self._cache_dir_path
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _chat_options(
        self,
        max_tokens: Optional[int],
        temperature: Optional[float],
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = dict(self.config.options)
        options["num_ctx"] = resolve_ollama_num_ctx(self.config)
        options["num_predict"] = max_tokens or self.config.max_tokens
        options["temperature"] = temperature if temperature is not None else self.config.temperature
        return options

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import ollama

            self._client = ollama
            return self._client

        except ImportError:
            raise ImportError("ollama not installed. Install with: pip install ollama")

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

        client = self._get_client()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat(
            model=self.config.model,
            messages=messages,
            options=self._chat_options(max_tokens, temperature),
        )

        content = response["message"]["content"]

        result = LLMResponse(
            content=content,
            model=self.config.model,
            tokens_used=response.get("eval_count", 0) + response.get("prompt_eval_count", 0),
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

    def is_available(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception as exc:
            # Any backend failure means "not available"; keep the broad net
            # but leave a diagnostic trail (#115).
            logger.debug("Ollama provider unavailable: %s", exc)
            return False

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "provider": "ollama",
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

    def _get_cache_key(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        combined = f"{system_prompt or ''}|{prompt}|{self.config.model}"
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
                logger.debug("Ignoring unreadable Ollama cache entry: %s", exc)
        return None

    def _save_to_cache(self, cache_key: str, response: LLMResponse):
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
            logger.debug("Could not write Ollama cache entry: %s", exc)
