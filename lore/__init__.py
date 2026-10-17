"""Lore — local-first AI coding-session history + shared agent memory.

The single source of truth for the package version. ``pyproject.toml``
reads ``__version__`` from here via ``[tool.setuptools.dynamic]``, so the
version is set in exactly one place.

2.4.0: the Python import package is ``lore``; CLI entry modules are
``lore_cli`` / ``lore_session_cli``; environment variables are ``LORE_*``.
Data dir is ``~/.lore``.
"""

__version__ = "2.4.0"
