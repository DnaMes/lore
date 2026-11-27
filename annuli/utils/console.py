"""Console safety: never crash while printing to a console that cannot encode a character."""

from __future__ import annotations

import sys


def make_console_safe() -> None:
    """Make stdout and stderr replace unencodable characters instead of raising.

    A Windows console on cp1252 cannot print the status symbols Lore uses, and a
    UnicodeEncodeError there turned every command into a traceback. Streams that cannot be
    reconfigured (pytest capture, some wrappers) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            continue
