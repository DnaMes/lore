"""The explicit provider request contract (#180).

One request is one :class:`ProviderRequest`. It names the provider identity, the
model, the authentication mode, the timeout, the cancellation token and the
execution policy — every field that decides where text goes and how the call is
bounded. Nothing is implied: a request without an auth mode, or with a mode the
selected provider does not implement, is refused by ``selection.select_provider``
with a typed error instead of being resolved by convention.

The descriptor is persistable: ``to_dict``/``from_dict`` round-trip it, and the
LLM cache stores it with every entry so an old artifact stays auditable after
the code or the prompt moved on. A cancellation token is a live object, so it is
persisted as its state (``cancellable``/``cancelled``) and rebuilt on load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from .errors import AdapterCancelled

# Bumped when a persisted field changes meaning, so an old descriptor is
# recognisable rather than silently misread.
REQUEST_SCHEMA_VERSION = 1

# ``LLMConfig.timeout`` defaults to this; spelled here so a request always carries
# its timeout instead of inheriting one implicitly.
DEFAULT_TIMEOUT_SECONDS = 60


class AuthMode(Enum):
    """How the selected provider authenticates. There is no implicit mode."""

    NATIVE_CLI = "native-cli"  # the installed CLI's own login; Lore holds nothing
    API_KEY = "api-key"
    OAUTH = "oauth"
    ADC = "adc"
    NONE = "none"  # loopback endpoint, no credential


class PromptTransport(Enum):
    """Where the prompt text travels."""

    STDIN = "stdin"
    ARGV = "argv"


class OutputFormat(Enum):
    """The structured-output contract the answer is parsed with."""

    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"


@dataclass(frozen=True)
class ExecutionPolicy:
    """What executing this request is allowed to do.

    An empty ``executable`` means the request runs in-process (HTTP/SDK) and no
    subprocess, argument vector or child environment exists at all.
    """

    executable: str = ""
    argv: tuple[str, ...] = ()
    prompt_transport: PromptTransport = PromptTransport.STDIN
    output_format: OutputFormat = OutputFormat.TEXT
    cwd: str = "tempdir"  # "tempdir" | "inherit"
    max_prompt_chars: int = 400_000
    kill_grace_seconds: float = 2.0

    @property
    def in_process(self) -> bool:
        return not self.executable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executable": self.executable,
            "argv": list(self.argv),
            "prompt_transport": self.prompt_transport.value,
            "output_format": self.output_format.value,
            "cwd": self.cwd,
            "max_prompt_chars": self.max_prompt_chars,
            "kill_grace_seconds": self.kill_grace_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionPolicy":
        return cls(
            executable=str(data.get("executable", "")),
            argv=tuple(data.get("argv", ())),
            prompt_transport=PromptTransport(data.get("prompt_transport", "stdin")),
            output_format=OutputFormat(data.get("output_format", "text")),
            cwd=str(data.get("cwd", "tempdir")),
            max_prompt_chars=int(data.get("max_prompt_chars", 400_000)),
            kill_grace_seconds=float(data.get("kill_grace_seconds", 2.0)),
        )


# A request executed in-process (HTTP/SDK providers): no subprocess, no argv.
IN_PROCESS_POLICY = ExecutionPolicy()


class CancellationToken:
    """Cooperative cancellation for one request.

    The adapter checks the token between polls of the child process, so setting
    it kills the process and raises :class:`AdapterCancelled` instead of letting
    the call finish and be discarded.
    """

    def __init__(self, cancelled: bool = False) -> None:
        import threading

        self._event = threading.Event()
        if cancelled:
            self._event.set()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self, provider: str) -> None:
        if self.cancelled:
            raise AdapterCancelled(provider)

    def to_dict(self) -> Dict[str, Any]:
        return {"cancellable": True, "cancelled": self.cancelled}


@dataclass(frozen=True)
class ProviderRequest:
    """One provider request, explicit and persistable (#180)."""

    provider: str
    model: str
    auth_mode: AuthMode
    timeout: float
    policy: ExecutionPolicy
    cancellation: CancellationToken = field(
        default_factory=CancellationToken, compare=False, repr=False
    )
    task: str = ""
    prompt_version: str = ""
    cache_scope: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """The persisted descriptor: everything but the token's liveness."""
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "provider": self.provider,
            "model": self.model,
            "auth_mode": self.auth_mode.value,
            "timeout": self.timeout,
            "cancellation": self.cancellation.to_dict(),
            "policy": self.policy.to_dict(),
            "task": self.task,
            "prompt_version": self.prompt_version,
            "cache_scope": self.cache_scope,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderRequest":
        cancellation = data.get("cancellation") or {}
        return cls(
            provider=str(data["provider"]),
            model=str(data.get("model", "")),
            auth_mode=AuthMode(data["auth_mode"]),
            timeout=float(data.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
            policy=ExecutionPolicy.from_dict(data.get("policy") or {}),
            cancellation=CancellationToken(cancelled=bool(cancellation.get("cancelled"))),
            task=str(data.get("task", "")),
            prompt_version=str(data.get("prompt_version", "")),
            cache_scope=str(data.get("cache_scope", "")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ProviderRequest":
        return cls.from_dict(json.loads(text))

    def cache_fields(self) -> Dict[str, Any]:
        """The part of the descriptor that changes what a cached answer means."""
        return {
            "provider": self.provider,
            "model": self.model,
            "auth_mode": self.auth_mode.value,
            "timeout": self.timeout,
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True)
class AdapterResult:
    """A successful native-CLI answer, parsed into the common job contract."""

    content: str
    model: str
    structured: Dict[str, Any] = field(default_factory=dict)
    tokens_used: int = 0
    duration_seconds: float = 0.0
    version: str = ""
    partial: bool = False


@dataclass(frozen=True)
class CliProbe:
    """What the installed executable is, and whether the documented contract is there."""

    provider: str
    executable: str
    available: bool
    path: Optional[str] = None
    version: str = ""
    capabilities: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "executable": self.executable,
            "available": self.available,
            "path": self.path or "",
            "version": self.version,
            "capabilities": list(self.capabilities),
            "reason": self.reason,
        }
