import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.paths import lore_home
from .base import LLMConfig, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None
        self._cache_dir = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir.mkdir(parents=True, exist_ok=True)

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
            options={
                "num_predict": max_tokens or self.config.max_tokens,
                "temperature": (
                    temperature if temperature is not None else self.config.temperature
                ),
            },
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
        cache_file = self._cache_dir / f"{cache_key}.json"
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
        cache_file = self._cache_dir / f"{cache_key}.json"
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
