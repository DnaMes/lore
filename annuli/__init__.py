"""Annuli — local-first AI coding-session history + shared agent memory.

The single source of truth for the package version. ``pyproject.toml``
reads ``__version__`` from here via ``[tool.setuptools.dynamic]``, so the
version is set in exactly one place.

2.4.0: the Python import package is ``annuli``; CLI entry modules are
``annuli_cli`` / ``annuli_session_cli``; environment variables are ``ANNULI_*``.
Data dir is ``~/.annuli``.
"""

import os
import sys

__version__ = "2.4.0"

_LEGACY_ENV_PREFIX = "LORE_"
_ENV_PREFIX = "ANNULI_"


def _adopt_legacy_env() -> None:
    """Copy pre-rebrand ``ANNULI_*`` variables to ``ANNULI_*`` unless already set.

    Child processes inherit the result. One deprecation line goes to stderr
    (never stdout: the MCP server speaks JSON-RPC there).
    """
    adopted = []
    for key, value in list(os.environ.items()):
        if key.startswith(_LEGACY_ENV_PREFIX):
            new_key = _ENV_PREFIX + key[len(_LEGACY_ENV_PREFIX) :]
            if new_key not in os.environ:
                os.environ[new_key] = value
                adopted.append(key)
    if adopted:
        print(
            "annuli: deprecated ANNULI_* variables read as ANNULI_*: " + ", ".join(sorted(adopted)),
            file=sys.stderr,
        )


_adopt_legacy_env()
