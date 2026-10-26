"""Provider construction: every choice goes through the explicit request contract.

Selection itself lives in :mod:`lore.llm.adapters.selection` (#180). This module
resolves the environment overrides it is responsible for, hands the request to
the cloud opt-in gate, and then builds one provider for that request — an
installed-CLI adapter, an in-process API client, or the local Ollama path. An
unknown name, an unsupported contract or a missing CLI never turns into a
different provider.
"""

import os
import sys
from typing import Optional

from .adapters import (
    CLI_ADAPTERS,
    HOSTED_DEFAULT_TIMEOUTS,
    PROVIDERS,
    AuthMode,
    CancellationToken,
    ProviderRequest,
    select_provider,
)
from .base import LLMConfig, LLMProvider
from .gate import CloudOptInRequired, ensure_cloud_allowed
from .gemini import GeminiProvider
from .jev import JevProvider
from .ollama import OllamaProvider
from .openrouter import OpenRouterProvider

TIMEOUT_ENV_VAR = "LORE_LLM_TIMEOUT"

# CloudOptInRequired is imported from .gate above (providers import it from there
# too, so the check lives outside this module and avoids an import cycle).

# Providers the factory itself knows; anything else is refused by selection, so an
# unknown name is never mistaken for an opt-in problem.
_KNOWN_PROVIDERS = frozenset(PROVIDERS)


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
    cancellation: Optional[CancellationToken] = None,
    auth_mode: Optional[AuthMode] = None,
    **kwargs,
) -> LLMProvider:
    """Build the provider named by an explicit :class:`ProviderRequest`.

    Raises :class:`~lore.llm.adapters.UnknownProviderError` (a ``ValueError``) for an
    unknown name, :class:`~lore.llm.adapters.UnsupportedProviderCombination` for a
    contract the provider does not implement (an API key offered to a native CLI,
    a mismatched auth mode, a ``base_url`` for a subprocess provider), and
    :class:`CloudOptInRequired` before any of that when the provider is non-local
    and the opt-in is missing. A missing CLI is *not* raised here: the adapter is
    built, ``is_available()`` is false, and its ``generate()`` raises
    ``AdapterUnavailable`` naming the reason.
    """
    name = (provider or "").strip().lower()
    if "timeout" not in kwargs:
        kwargs = _resolve_timeout(name, kwargs)
    request: ProviderRequest = select_provider(
        name,
        model=model,
        api_key=api_key,
        auth_mode=auth_mode,
        timeout=kwargs.get("timeout"),
        cancellation=cancellation,
        base_url=kwargs.get("base_url"),
    )

    refusal = _cloud_opt_in_error(request.provider, allow_cloud)
    if refusal:
        raise CloudOptInRequired(refusal)

    # ``timeout`` is part of the request now; never pass it twice to LLMConfig.
    config_kwargs = {key: value for key, value in kwargs.items() if key != "timeout"}

    adapter_cls = CLI_ADAPTERS.get(request.provider)
    if adapter_cls is not None:
        config = LLMConfig(
            provider=request.provider,
            model=request.model,
            timeout=request.timeout,
            **config_kwargs,
        )
        return adapter_cls(config, allow_cloud=allow_cloud, request=request)

    if request.provider == "gemini":
        config = LLMConfig(
            provider="gemini",
            model=request.model,
            api_key=api_key,
            timeout=request.timeout,
            **config_kwargs,
        )
        return GeminiProvider(config, allow_cloud=allow_cloud)

    elif request.provider == "ollama":
        config = LLMConfig(
            provider="ollama",
            model=request.model,
            timeout=request.timeout,
            **config_kwargs,
        )
        return OllamaProvider(config, allow_cloud=allow_cloud)

    elif request.provider == "openrouter":
        config = LLMConfig(
            provider="openrouter",
            model=request.model,
            api_key=api_key,
            timeout=request.timeout,
            **config_kwargs,
        )
        return OpenRouterProvider(config, allow_cloud=allow_cloud)

    elif request.provider == "jev":
        config = LLMConfig(
            provider="jev",
            model=request.model,
            timeout=request.timeout,
            **config_kwargs,
        )
        return JevProvider(config, allow_cloud=allow_cloud)

    # Selection and construction read the same table; reaching this line means
    # they disagree, which is a bug rather than an unknown name.
    raise AssertionError(f"provider '{request.provider}' has no constructor")
