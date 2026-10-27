"""Atomic provider-cache writes and the cleanup a hard kill leaves behind.

Every cache entry is written to a hidden ``.<cache key>.<random>.tmp`` file in the
cache directory and then renamed into place, so a reader never sees half a JSON
document. The temp file is unlinked on any error the process survives — but a
``SIGKILL`` between ``mkstemp`` and the rename leaves it behind forever, and the
provider caches never delete an entry they did not write, so nothing else would
ever remove it.

:func:`sweep_temp_files` removes those leftovers. The providers call it when they
open their cache directory, which happens once per provider instance.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Prefix and suffix of the temp files the providers create (see ``_save_to_cache``).
TEMP_PREFIX = "."

#: A temp file younger than this may belong to a write still in flight; leave it alone.
SWEEP_AGE_SECONDS = 3600.0


def sweep_temp_files(
    cache_root: Path,
    *,
    max_age_seconds: float = SWEEP_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Delete leftover provider-cache temp files; return how many were removed.

    Only top-level, hidden ``*.tmp`` files older than ``max_age_seconds`` go: a
    fresh temp file is somebody's write in progress, and a subdirectory is not
    something this code ever creates.
    """
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    try:
        entries = list(os.scandir(cache_root))
    except OSError:
        # Missing or unreadable cache directory: nothing to clean, never an error.
        return 0
    for entry in entries:
        name = entry.name
        if not (name.startswith(TEMP_PREFIX) and name.endswith(".tmp")):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            os.unlink(entry.path)
        except OSError:
            continue
        removed += 1
    return removed
