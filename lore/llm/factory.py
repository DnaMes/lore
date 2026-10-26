import os
import sys
from typing import Optional

from .base import LLMConfig, LLMProvider
from .gate import CloudOptInRequired, ensure_cloud_allowed
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


# CloudOptInRequired is imported from .gate above (providers import it from there
# too, so the check lives outside this module and avoids an import cycle).

# Providers the factory itself knows; anything else keeps the ValueError below so an
# unknown name is never mistaken for an opt-in problem.
_KNOWN_PROVIDERS = frozenset({"gemini", "ollama", "openrouter", "jev"})


def _cloud_opt_in_error(provider: str, allow_cloud: bool) -> str | None:
    """Return the refusal message when ``provider`` is non-local without opt-in."""
    if provider not in _KNOWN_PROVIDERS:
        return None
    # One shared check with the provider transport boundary (lore/llm/gate.py).
    try:
        ensure_cloud_allowed(provider, allow_cloud)
    except CloudOptInRequired as exc:
        return str(exc)
    return None


def get_provider(
    provider: str = "gemini",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    allow_cloud: bool = False,
    **kwargs,
) -> LLMProvider:
    provider = provider.lower()

    refusal = _cloud_opt_in_error(provider, allow_cloud)
    if refusal:
        raise CloudOptInRequired(refusal)

    if provider == "gemini":
        config = LLMConfig(
            provider="gemini",
            model=model or "gemini-2.0-flash",
            api_key=api_key,
            **kwargs,
        )
        return GeminiProvider(config, allow_cloud=allow_cloud)

    elif provider == "ollama":
        config = LLMConfig(
            provider="ollama",
            model=model or "llama3.2",
            **kwargs,
        )
        return OllamaProvider(config, allow_cloud=allow_cloud)

    elif provider == "openrouter":
        default_model = os.environ.get("LORE_OPENROUTER_MODEL") or DEFAULT_MODEL
        config = LLMConfig(
            provider="openrouter",
            model=model or default_model,
            api_key=api_key,
            **_resolve_timeout(provider, kwargs),
        )
        return OpenRouterProvider(config, allow_cloud=allow_cloud)

    elif provider == "jev":
        config = LLMConfig(
            provider="jev",
            model=model or DEFAULT_MODEL_LABEL,
            **_resolve_timeout(provider, kwargs),
        )
        return JevProvider(config, allow_cloud=allow_cloud)

    else:
        raise ValueError(
            f"Unknown provider: {provider}. Supported: gemini, jev, ollama, openrouter"
        )
