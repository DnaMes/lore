"""Provider selection (#180): one explicit contract per provider name.

The table in this module is the only place that knows which provider names exist,
what each one authenticates with, which model answers when none is named, and how
long a call may take. ``factory.get_provider`` and the CLI's default-model lookup
both read it, so a name cannot mean one thing in one place and another somewhere
else.

Selection refuses contradictions with typed errors (``errors``): an unknown name,
an auth mode the selected provider does not implement, an API key offered to a
provider that only has a native CLI login, or a ``base_url`` for a provider that
runs a subprocess. None of them falls back to a different provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Type

from ..jev import DEFAULT_MODEL_LABEL
from ..openrouter import DEFAULT_MODEL as OPENROUTER_DEFAULT_MODEL
from .antigravity import AntigravityCliAdapter
from .claude import ClaudeCliAdapter
from .codex import CodexCliAdapter
from .contract import (
    DEFAULT_TIMEOUT_SECONDS,
    IN_PROCESS_POLICY,
    AuthMode,
    CancellationToken,
    ExecutionPolicy,
    ProviderRequest,
)
from .errors import (
    ProviderSelectionError,
    UnknownProviderError,
    UnsupportedProviderCombination,
)
from .gemini import GeminiCliAdapter
from .native_cli import NativeCliAdapter

# Hosted providers get more than the LLMConfig default: a delegate hop chain and
# a free-tier OpenRouter model both routinely exceed 60 s.
HOSTED_DEFAULT_TIMEOUTS = {"openrouter": 90, "jev": 180}


def _constant(value: str) -> Callable[[], str]:
    return lambda: value


def _openrouter_default_model() -> str:
    return os.environ.get("LORE_OPENROUTER_MODEL") or OPENROUTER_DEFAULT_MODEL


@dataclass(frozen=True)
class ProviderSpec:
    """What one provider name means: auth, default model, timeout, execution."""

    name: str
    auth_mode: AuthMode
    default_model: Callable[[], str]
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    adapter: Optional[Type[NativeCliAdapter]] = None

    @property
    def is_native_cli(self) -> bool:
        return self.adapter is not None

    def resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model()

    def execution_policy(self) -> ExecutionPolicy:
        if self.adapter is None:
            return IN_PROCESS_POLICY
        return self.adapter.execution_policy()


PROVIDERS: Dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec("gemini", AuthMode.API_KEY, _constant("gemini-2.0-flash")),
        ProviderSpec("ollama", AuthMode.NONE, _constant("llama3.2")),
        ProviderSpec(
            "openrouter",
            AuthMode.API_KEY,
            _openrouter_default_model,
            timeout=HOSTED_DEFAULT_TIMEOUTS["openrouter"],
        ),
        ProviderSpec(
            "jev",
            AuthMode.NONE,
            _constant(DEFAULT_MODEL_LABEL),
            timeout=HOSTED_DEFAULT_TIMEOUTS["jev"],
        ),
        ProviderSpec(
            "claude-cli",
            AuthMode.NATIVE_CLI,
            _constant(ClaudeCliAdapter.DEFAULT_MODEL),
            adapter=ClaudeCliAdapter,
        ),
        ProviderSpec(
            "codex-cli",
            AuthMode.NATIVE_CLI,
            _constant(CodexCliAdapter.DEFAULT_MODEL),
            adapter=CodexCliAdapter,
        ),
        ProviderSpec(
            "gemini-cli",
            AuthMode.NATIVE_CLI,
            _constant(GeminiCliAdapter.DEFAULT_MODEL),
            adapter=GeminiCliAdapter,
        ),
        ProviderSpec(
            "antigravity-cli",
            AuthMode.NATIVE_CLI,
            _constant(AntigravityCliAdapter.DEFAULT_MODEL),
            adapter=AntigravityCliAdapter,
        ),
    )
}

# Providers answered by an installed CLI rather than by an in-process client.
CLI_ADAPTERS: Dict[str, Type[NativeCliAdapter]] = {
    name: spec.adapter for name, spec in PROVIDERS.items() if spec.adapter is not None
}


def provider_names() -> str:
    """The supported provider names, sorted, for error and help text."""
    return ", ".join(sorted(PROVIDERS))


def default_model_for(provider: str) -> str:
    """Default model label for ``provider``; ``""`` means the CLI's own default."""
    spec = PROVIDERS.get((provider or "").strip().lower())
    if spec is None:
        raise UnknownProviderError(f"Unknown provider: {provider}. Supported: {provider_names()}")
    return spec.default_model()


def _coerce_auth_mode(auth_mode: "AuthMode | str | None") -> Optional[AuthMode]:
    if auth_mode is None or isinstance(auth_mode, AuthMode):
        return auth_mode
    try:
        return AuthMode(str(auth_mode).strip().lower())
    except ValueError:
        supported = ", ".join(mode.value for mode in AuthMode)
        raise UnsupportedProviderCombination(
            f"unknown auth mode {auth_mode!r}; supported: {supported}"
        ) from None


def select_provider(
    provider: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    auth_mode: "AuthMode | str | None" = None,
    timeout: Optional[float] = None,
    cancellation: Optional[CancellationToken] = None,
    base_url: Optional[str] = None,
    task: str = "",
    prompt_version: str = "",
    cache_scope: str = "",
) -> ProviderRequest:
    """Turn a provider name into the explicit request contract, or refuse it."""
    name = (provider or "").strip().lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise UnknownProviderError(f"Unknown provider: {name}. Supported: {provider_names()}")

    requested_auth = _coerce_auth_mode(auth_mode)
    if requested_auth is not None and requested_auth is not spec.auth_mode:
        raise UnsupportedProviderCombination(
            f"provider '{name}' authenticates with '{spec.auth_mode.value}'; "
            f"'{requested_auth.value}' is not supported for it"
        )

    if spec.is_native_cli:
        if api_key is not None:
            raise UnsupportedProviderCombination(
                f"provider '{name}' answers through the installed CLI's own login; "
                "Lore does not accept or pass an API key for it"
            )
        if base_url is not None:
            raise UnsupportedProviderCombination(
                f"provider '{name}' runs a local executable; a base_url is not supported for it"
            )

    if timeout is None:
        resolved_timeout = spec.timeout
    else:
        resolved_timeout = float(timeout)
        if resolved_timeout <= 0:
            raise ProviderSelectionError(
                f"timeout must be a positive number of seconds, got {timeout!r}"
            )

    return ProviderRequest(
        provider=name,
        model=spec.resolve_model(model),
        auth_mode=spec.auth_mode,
        timeout=resolved_timeout,
        policy=spec.execution_policy(),
        cancellation=cancellation or CancellationToken(),
        task=task,
        prompt_version=prompt_version,
        cache_scope=cache_scope,
    )


__all__ = [
    "CLI_ADAPTERS",
    "HOSTED_DEFAULT_TIMEOUTS",
    "PROVIDERS",
    "ProviderSpec",
    "default_model_for",
    "provider_names",
    "select_provider",
]
