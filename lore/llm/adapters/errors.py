"""Typed failures at the provider boundary (#180).

Each class names one condition a caller can act on. Nothing here degrades into a
different provider: an unavailable CLI, a timeout, a cancellation, a refused
login and an unparsable answer are separate outcomes, and the selection errors
refuse an unknown or contradictory request before anything runs.
"""

from __future__ import annotations


class AdapterError(RuntimeError):
    """Base class for every native-CLI adapter failure."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")


class AdapterUnavailable(AdapterError):
    """The selected executable is missing, unrunnable, or lacks the documented interface."""

    def __init__(self, provider: str, reason: str, *, version: str = "") -> None:
        self.reason = reason
        self.version = version
        super().__init__(provider, f"unavailable: {reason}" if reason else "unavailable")


class AdapterTimeout(AdapterError):
    """The call exceeded the request's ``timeout`` and the process was killed."""

    def __init__(self, provider: str, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(provider, f"timed out after {timeout:g}s")


class AdapterCancelled(AdapterError):
    """The request's cancellation token was set and the process was killed."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider, "cancelled by the caller")


class AdapterAuthError(AdapterError):
    """The CLI reported a missing or rejected login (Lore never supplies one)."""


class AdapterExecutionError(AdapterError):
    """The CLI exited non-zero or reported a failed result."""

    def __init__(self, provider: str, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        if returncode == 0:
            detail = f"reported a failed result: {stderr}" if stderr else "reported a failed result"
        else:
            detail = f"exited {returncode}" + (f": {stderr}" if stderr else "")
        super().__init__(provider, detail)


class AdapterOutputError(AdapterError):
    """The documented structured output was absent or malformed."""


class AdapterPromptTooLarge(AdapterError):
    """The composed prompt exceeds the execution policy's bound."""

    def __init__(self, provider: str, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(provider, f"prompt is {size} chars, over the {limit} char bound")


class ProviderSelectionError(ValueError):
    """The requested provider or request contract cannot be satisfied.

    Subclasses ``ValueError`` so existing callers that treat an unknown provider
    name as a bad argument keep working.
    """


class UnknownProviderError(ProviderSelectionError):
    """No spec exists for the requested provider name."""


class UnsupportedProviderCombination(ProviderSelectionError):
    """A known provider was asked for a contract it does not implement."""
