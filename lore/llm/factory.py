import os
import sys
from typing import Optional

from .base import LLMConfig, LLMProvider
from .gemini import GeminiProvider
from .jev import DEFAULT_MODEL_LABEL, JevProvider
from .ollama import OllamaProvider
from .openrouter import DEFAULT_MODEL, OpenRouterProvider

# Hosted providers wait longer than the 60 s LLMConfig default: a delegate hop
# chain and a free-tier OpenRouter model both routinely exceed it.
HOSTED_DEFAULT_TIMEOUTS = {"openrouter": 90, "jev": 180}
TIMEOUT_ENV_VAR = "LORE_LLM_TIMEOUT"


def _resolve_timeout(provider: str, kwargs: dict) -> dict:
    """Apply ``LORE_LLM_TIMEOUT`` and the hosted defaults to ``kwargs``.

    Only ``openrouter`` and ``jev`` are affected — local providers keep
    ``LLMConfig.timeout``. An explicit ``timeout`` argument wins over the env
    var, and an unparsable value falls back to the provider default with a
    warning on stderr instead of failing the call.
    """
    default = HOSTED_DEFAULT_TIMEOUTS.get(provider)
    if default is None or "timeout" in kwargs:
        return kwargs
    raw = os.environ.get(TIMEOUT_ENV_VAR)
    if raw is None:
        return {**kwargs, "timeout": default}
    try:
        seconds = int(raw)
        if seconds <= 0:
            raise ValueError
    except ValueError:
        print(
            f"warning: ignoring invalid {TIMEOUT_ENV_VAR}={raw!r}; using {default}s",
            file=sys.stderr,
        )
        return {**kwargs, "timeout": default}
    return {**kwargs, "timeout": seconds}


def get_provider(
    provider: str = "gemini",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    **kwargs,
) -> LLMProvider:
    provider = provider.lower()

    if provider == "gemini":
        config = LLMConfig(
            provider="gemini",
            model=model or "gemini-2.0-flash",
            api_key=api_key,
            **kwargs,
        )
        return GeminiProvider(config)

    elif provider == "ollama":
        config = LLMConfig(
            provider="ollama",
            model=model or "llama3.2",
            **kwargs,
        )
        return OllamaProvider(config)

    elif provider == "openrouter":
        default_model = os.environ.get("LORE_OPENROUTER_MODEL") or DEFAULT_MODEL
        config = LLMConfig(
            provider="openrouter",
            model=model or default_model,
            api_key=api_key,
            **_resolve_timeout(provider, kwargs),
        )
        return OpenRouterProvider(config)

    elif provider == "jev":
        config = LLMConfig(
            provider="jev",
            model=model or DEFAULT_MODEL_LABEL,
            **_resolve_timeout(provider, kwargs),
        )
        return JevProvider(config)

    else:
        raise ValueError(
            f"Unknown provider: {provider}. Supported: gemini, jev, ollama, openrouter"
        )
