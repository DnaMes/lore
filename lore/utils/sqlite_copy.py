"""Copy SQLite databases with WAL/SHM sidecars for safe read-only opens."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

_CHUNK = 1024 * 1024


def _copy_regular_file(src: Path, dest: Path, remaining: int) -> Optional[int]:
    """Copy ``src`` to ``dest`` through one descriptor, never following a symlink.

    The file is opened with O_NOFOLLOW and inspected with fstat on that same descriptor,
    so a swap between the check and the copy cannot redirect the read. Returns the
    number of bytes copied, or None when the file is not a regular file or exceeds
    ``remaining`` bytes.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(src, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > remaining:
            return None
        copied = 0
        with open(dest, "wb") as out:
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > remaining:  # grew while copying
                    return None
                out.write(chunk)
        return copied
    except OSError:
        return None
    finally:
        os.close(fd)


def copy_sqlite_with_sidecars(db_path: Path, tmp_dir: Path, max_bytes: int) -> Optional[Path]:
    """Copy the database and its -wal/-shm files; None if any file is unsafe or too big.

    The database must exist and be a plain file; absent sidecars are fine. The combined
    size of everything copied is capped at ``max_bytes``.
    """
    remaining = max_bytes
    for suffix in ("", "-wal", "-shm"):
        src = db_path if suffix == "" else Path(f"{db_path}{suffix}")
        if suffix and not os.path.lexists(src):
            continue
        copied = _copy_regular_file(src, tmp_dir / f"{db_path.name}{suffix}", remaining)
        if copied is None:
            return None
        remaining -= copied
    return tmp_dir / db_path.name
