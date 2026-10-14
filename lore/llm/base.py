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

    def _get_cache_key(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        import hashlib

        combined = f"{system_prompt or ''}|{prompt}"
        return hashlib.sha256(combined.encode()).hexdigest()
