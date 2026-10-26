"""Explicit provider boundary for native-CLI and local-model providers (#180).

``lore.llm.factory`` and the adapters import from here; nothing in this package
is a second opinion about where text goes.

* :mod:`~lore.llm.adapters.contract` — the request descriptor: provider identity,
  model, auth mode, timeout, cancellation token, execution policy. Persistable.
* :mod:`~lore.llm.adapters.errors` — typed outcomes: unavailable, auth, timeout,
  cancelled, execution error, malformed output, refused selection.
* :mod:`~lore.llm.adapters.native_cli` — detection (``which`` + ``--version`` +
  capability probe), bounded invocation, cancellation, parsed results.
* :mod:`~lore.llm.adapters.selection` — the one table of provider names and what
  each means.
* ``codex``, ``claude``, ``gemini``, ``antigravity`` — the installed-CLI adapters
  (#181, #182, #184, #183).
"""

from .antigravity import AntigravityCliAdapter
from .claude import ClaudeCliAdapter
from .codex import CodexCliAdapter
from .contract import (
    REQUEST_SCHEMA_VERSION,
    AdapterResult,
    AuthMode,
    CancellationToken,
    CliProbe,
    ExecutionPolicy,
    OutputFormat,
    PromptTransport,
    ProviderRequest,
)
from .errors import (
    AdapterAuthError,
    AdapterCancelled,
    AdapterError,
    AdapterExecutionError,
    AdapterOutputError,
    AdapterPromptTooLarge,
    AdapterTimeout,
    AdapterUnavailable,
    ProviderSelectionError,
    UnknownProviderError,
    UnsupportedProviderCombination,
)
from .gemini import GeminiCliAdapter
from .native_cli import CREDENTIAL_ENV_VARS, NativeCliAdapter
from .selection import (
    CLI_ADAPTERS,
    HOSTED_DEFAULT_TIMEOUTS,
    PROVIDERS,
    ProviderSpec,
    default_model_for,
    provider_names,
    select_provider,
)

__all__ = [
    "AdapterAuthError",
    "AdapterCancelled",
    "AdapterError",
    "AdapterExecutionError",
    "AdapterOutputError",
    "AdapterPromptTooLarge",
    "AdapterResult",
    "AdapterTimeout",
    "AdapterUnavailable",
    "AntigravityCliAdapter",
    "AuthMode",
    "CLI_ADAPTERS",
    "CREDENTIAL_ENV_VARS",
    "CancellationToken",
    "ClaudeCliAdapter",
    "CliProbe",
    "CodexCliAdapter",
    "ExecutionPolicy",
    "GeminiCliAdapter",
    "HOSTED_DEFAULT_TIMEOUTS",
    "NativeCliAdapter",
    "OutputFormat",
    "PROVIDERS",
    "PromptTransport",
    "ProviderRequest",
    "ProviderSelectionError",
    "ProviderSpec",
    "REQUEST_SCHEMA_VERSION",
    "UnknownProviderError",
    "UnsupportedProviderCombination",
    "default_model_for",
    "provider_names",
    "select_provider",
]
