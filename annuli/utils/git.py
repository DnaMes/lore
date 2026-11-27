"""Git utilities for linking sessions to repository state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def get_git_info(project_path: Optional[str]) -> dict:
    """Return git branch and HEAD SHA for a project directory.

    Returns {"branch": None, "sha": None} if not a git repo or git unavailable.
    """
    if not project_path:
        return {"branch": None, "sha": None}
    path = Path(project_path).expanduser()
    if not path.exists():
        return {"branch": None, "sha": None}
    try:
        branch = (
            subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode()
            .strip()
        )
        sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=path,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode()
            .strip()
        )
        return {"branch": branch, "sha": sha}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {"branch": None, "sha": None}
