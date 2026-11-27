import os
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import List


def _scan_depth(default: int) -> int:
    raw = os.environ.get("LORE_HOME_SCAN_DEPTH", str(default)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _scan_limit(default: int) -> int:
    raw = os.environ.get("LORE_HOME_SCAN_LIMIT", str(default)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _scan_node_limit(default: int) -> int:
    raw = os.environ.get("LORE_HOME_SCAN_NODE_LIMIT", str(default)).strip()
    try:
        return max(100, int(raw))
    except ValueError:
        return default


def _scan_time_budget_seconds(default: float) -> float:
    raw = os.environ.get("LORE_HOME_SCAN_TIME_BUDGET_SECONDS", str(default)).strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


@lru_cache(maxsize=64)
def _discover_cached(home: str, marker: str, max_depth: int, max_hits: int) -> tuple[str, ...]:
    home_path = Path(home).expanduser()
    marker_rel = marker.strip("/")
    if not marker_rel or not home_path.exists():
        return ()

    skip_dirnames = {
        ".cache",
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "Downloads",
        "Library",
        "dist",
        "build",
        "target",
    }

    discovered: List[str] = []
    queue = deque([(home_path, 0)])
    seen: set[str] = set()
    visited_nodes = 0
    node_limit = _scan_node_limit(25000)
    deadline = time.monotonic() + _scan_time_budget_seconds(10.0)

    while queue and len(discovered) < max_hits:
        if visited_nodes >= node_limit or time.monotonic() >= deadline:
            break
        current, depth = queue.popleft()
        current_key = str(current)
        if current_key in seen:
            continue
        seen.add(current_key)
        visited_nodes += 1

        candidate = current / marker_rel
        try:
            candidate_exists = candidate.exists()
        except OSError:
            candidate_exists = False
        if candidate_exists:
            try:
                resolved = str(candidate.resolve())
            except Exception:
                resolved = str(candidate)
            if resolved not in discovered:
                discovered.append(resolved)
                if len(discovered) >= max_hits:
                    break

        if depth >= max_depth:
            continue

        try:
            entries = list(current.iterdir())
        except OSError:
            continue

        for entry in entries:
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
            except OSError:
                # Listable but not searchable (mode r--): skip instead of
                # aborting the whole export.
                continue
            if entry.name in skip_dirnames:
                continue
            queue.append((entry, depth + 1))

    return tuple(discovered)


def discover_home_marker_paths(
    marker: str, default_depth: int = 4, default_limit: int = 64
) -> List[Path]:
    max_depth = _scan_depth(default_depth)
    max_hits = _scan_limit(default_limit)
    paths = _discover_cached(str(Path.home()), marker, max_depth, max_hits)
    return [Path(path) for path in paths]
