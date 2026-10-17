import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

_HOME_NAME = ".lore"


def lore_home() -> Path:
    """Return Lore's data directory (``~/.lore``)."""
    return Path.home() / _HOME_NAME


def secure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the owner (0700).

    The output directory holds full session transcripts and the v2 SQLite
    store — sensitive on a multi-user host. Best-effort: a chmod failure
    (e.g. on a filesystem without POSIX modes) is ignored (#41).
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def restrict_file(path: Path) -> None:
    """Restrict a file to owner read/write (0600). Best-effort (#41).

    Used for data files that contain session content — index.json, the
    deleted-sessions list, the v2 SQLite database.
    """
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def get_current_project(cwd: Optional[Path] = None) -> str:
    """Detect current project directory."""
    if cwd is None:
        cwd = Path.cwd()

    # Try to find git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return str(cwd)


def sanitize_filename(name: str) -> str:
    """Sanitize string for use as filename."""
    # Remove or replace invalid characters
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "-", name)
    name = name[:50]  # Limit length
    return name or "untitled"


def project_to_dirname(project_path: Optional[str]) -> str:
    """Convert project path to directory name."""
    if not project_path:
        return "_unknown"
    # /home/user/projects/foo -> home-user-projects-foo
    clean = project_path.strip("/").replace("/", "-")
    return clean or "_unknown"


def make_thread_id(
    project_path: Optional[str] = None, project_hash: Optional[str] = None
) -> Optional[str]:
    """Create a stable thread id for cross-tool continuity."""
    if project_path:
        digest = hashlib.sha256(project_path.encode()).hexdigest()
        return f"project:{digest}"
    if project_hash:
        return f"project:{project_hash}"
    return None


def safe_copy_db(source_path: Path) -> Path:
    """Copy database to temp dir to avoid locking/permission issues."""
    try:
        if not source_path.exists():
            return source_path

        temp_dir = Path(tempfile.gettempdir())
        suffix = f"_{uuid.uuid4().hex}"
        dest_path = temp_dir / f"copy_{source_path.stem}{suffix}{source_path.suffix}"

        # Copy file
        shutil.copy2(source_path, dest_path)

        # Try to copy WAL/SHM files if they exist
        # Note: source_path is like 'state.vscdb', wal is 'state.vscdb-wal'
        # The logic source_path.with_suffix(...) replaces suffix, we want to append

        wal_source = Path(str(source_path) + "-wal")
        shm_source = Path(str(source_path) + "-shm")

        if wal_source.exists():
            shutil.copy2(wal_source, str(dest_path) + "-wal")
        if shm_source.exists():
            shutil.copy2(shm_source, str(dest_path) + "-shm")

        return dest_path
    except Exception as e:
        print(f"Warning: Failed to copy DB {source_path}: {e}", file=sys.stderr)
        return source_path
