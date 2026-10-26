from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional

from ..utils.redact import redact


class TaskType(Enum):
    TITLE_GENERATION = "title_generation"
    SESSION_FORMAT = "session_format"
    STATISTICS = "statistics"
    KNOWLEDGE_EXTRACTION = "knowledge_extraction"
    SUMMARIZATION = "summarization"
    TAGGING = "tagging"


@dataclass
class LLMConfig:
    provider: str = "gemini"
    model: str = "gemini-2.0-flash"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout: int = 60
    cache_enabled: bool = True
    cache_dir: Optional[str] = None
    # OAuth2 credentials (alternative to api_key)
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    oauth_refresh_token: Optional[str] = None
    # Use Application Default Credentials (ADC)
    use_adc: bool = False
    # Appended last so positional construction of the older fields keeps working.
    options: Dict[str, Any] = field(default_factory=dict)
    num_ctx: Optional[int] = None
    # Cache identity (#186): what the request *is*, not just its text. Appended
    # last for the same positional-compatibility reason; a missing value simply
    # makes the cache key coarser, which is the old behaviour.
    task: str = ""
    prompt_version: str = ""
    cache_scope: str = ""

    @classmethod
    def for_transcripts(cls, **overrides: Any) -> "LLMConfig":
        """Config for prompts that include session transcript text.

        Disk caching is always disabled. No environment variable re-enables
        caching for transcript-bearing tasks (privacy).
        """
        opts = dict(overrides)
        opts.pop("cache_enabled", None)
        return cls(**opts, cache_enabled=False)


def transcript_safe(provider: "LLMProvider") -> "LLMProvider":
    """Disable the disk cache on ``provider`` before it carries transcript text."""
    if provider.config.cache_enabled:
        provider.config = replace(provider.config, cache_enabled=False)
    return provider


def prepare_prompt(text: str) -> tuple[str, int]:
    """Redact secret-shaped strings from user prompt text before sending to a provider."""
    result = redact(text)
    return result.text, result.count


@dataclass
class LLMResponse:
    content: str
    model: str
    tokens_used: int = 0
    cached: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


def _fingerprint(text: str) -> str:
    """SHA-256 of a text block, so the key stays readable and bounded."""
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class LLMProvider(ABC):
    def __init__(self, config: LLMConfig):
        self.config = config
        self._cache: Dict[str, LLMResponse] = {}

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        pass

    @abstractmethod
    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[LLMResponse]:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        pass

    def provider_identity(self) -> Dict[str, Any]:
        """Who answers this provider's requests, and how it authenticates (#180).

        ``cache_identity`` stays the finer-grained key of one request; this is the
        stable description of the destination. Native-CLI adapters add the
        executable, its version and the auth mode.
        """
        return {"provider": type(self).__name__, "model": self.config.model or ""}

    # Bumped whenever the key composition changes, so old entries simply stop
    # matching instead of being mistaken for a hit of a different request (#186).
    CACHE_KEY_VERSION = 2

    def cache_identity(self, prompt: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Everything a cached result depends on (#186).

        The key must change when any of these change, and only then: the source
        revision (``cache_scope`` — the session revision, chunk range or coverage
        the request was built from), the effective provider and model, the
        sampling parameters, the prompt version, the task type, and the prompt
        itself. Sorted and JSON-encoded before hashing so the key is stable.
        """
        options = self.config.options if isinstance(self.config.options, dict) else {}
        return {
            "key_version": self.CACHE_KEY_VERSION,
            "provider": type(self).__name__,
            "model": self.config.model or "",
            "task": self.config.task or "",
            "prompt_version": self.config.prompt_version or "",
            "cache_scope": self.config.cache_scope or "",
            "params": {
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "num_ctx": self.config.num_ctx,
                "options": {str(k): options[k] for k in sorted(options)},
                "base_url": self.config.base_url or "",
            },
            "system": _fingerprint(system_prompt or ""),
            "prompt": _fingerprint(prompt),
            **self._cache_identity_extras(),
        }

    def _cache_identity_extras(self) -> Dict[str, Any]:
        """Provider-specific key material (e.g. the delegate lane list of ``jev``)."""
        return {}

    def _get_cache_key(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        import hashlib
        import json

        payload = json.dumps(
            self.cache_identity(prompt, system_prompt), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
