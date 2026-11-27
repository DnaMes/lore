"""Shared cloud opt-in enforcement at the provider transport boundary.

``CloudOptInRequired`` lives here (not in ``factory``) so every provider module can
import the check without an import cycle: the factory imports the providers, so the
providers must not import the factory. The locality/opt-in helpers are imported
lazily from ``summarize_cli`` for the same reason the factory does it — that module
sits above the LLM layer in the dependency graph.
"""

from __future__ import annotations


class CloudOptInRequired(RuntimeError):
    """A non-local provider was requested without ``--allow-cloud``/``LORE_ALLOW_CLOUD=1``."""


def ensure_cloud_allowed(provider: str, allow_cloud: bool = False) -> None:
    """Raise :class:`CloudOptInRequired` unless ``provider`` is local or opt-in is granted.

    Opt-in is ``allow_cloud=True`` or ``LORE_ALLOW_CLOUD`` in ``1``/``true``/``yes``.
    Locality comes from the shared CLI helper, so a remote ``OLLAMA_HOST`` counts as
    cloud here exactly like it does on the command line.
    """
    # Imported lazily: see the module docstring.
    from ..services.summarize_cli import cloud_opt_in_granted, provider_locality

    if provider_locality(provider) != "cloud":
        return
    if cloud_opt_in_granted(allow_cloud):
        return
    raise CloudOptInRequired(
        f"provider '{provider}' sends session text to the cloud; "
        "pass allow_cloud=True, --allow-cloud or set LORE_ALLOW_CLOUD=1 to confirm"
    )
