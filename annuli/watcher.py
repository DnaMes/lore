"""File watcher for auto-sync when AI tool data changes.

Polls the data directories exposed by each registered extractor and fires a
callback when any of them changes (mtime bump or new file).

Intentionally simple: polling-based, no inotify/watchdog dependency. If the
optional ``watchdog`` package is installed we still poll — the dependency is
declared only for users who want to wire up a more reactive listener
themselves.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# Attributes commonly exposed by extractors that point at on-disk data.
_PATH_ATTRS: tuple[str, ...] = (
    "base_path",
    "base_paths",
    "db_path",
    "db_paths",
    "workspace_storage",
    "workspace_roots",
    "storage_path",
    "storage_paths",
)


def _iter_paths(value: object) -> Iterable[Path]:
    if value is None:
        return ()
    if isinstance(value, Path):
        return (value,)
    if isinstance(value, str):
        return (Path(value),)
    if isinstance(value, (list, tuple, set)):
        paths: list[Path] = []
        for item in value:
            if isinstance(item, Path):
                paths.append(item)
            elif isinstance(item, str):
                paths.append(Path(item))
        return paths
    return ()


def collect_extractor_paths(extractors: Iterable[object]) -> list[Path]:
    """Pull all known data-directory paths off the given extractors.

    Duplicates and non-existent paths are filtered out. Order is preserved so
    the watcher snapshot is deterministic.
    """

    seen: set[Path] = set()
    paths: list[Path] = []
    for extractor in extractors:
        for attr in _PATH_ATTRS:
            value = getattr(extractor, attr, None)
            for path in _iter_paths(value):
                try:
                    resolved = path.expanduser()
                except (RuntimeError, OSError):
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                if resolved.exists():
                    paths.append(resolved)
    return paths


class SessionWatcher:
    """Polls known AI tool data directories for changes, triggers callback.

    The callback runs on the watcher thread; it should be fast and thread-safe
    (e.g. ``clear_index_cache``). Exceptions inside the callback are logged
    and swallowed so a single failure doesn't kill the watcher.
    """

    def __init__(
        self,
        callback: Callable[[], None],
        interval: float = 30.0,
        paths: Iterable[Path] | None = None,
    ) -> None:
        self._callback = callback
        self._interval = max(1.0, float(interval))
        self._explicit_paths = list(paths) if paths is not None else None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_mtimes: dict[Path, float] = {}

    def _get_watch_paths(self) -> list[Path]:
        """Return paths to watch based on available extractors."""
        if self._explicit_paths is not None:
            return [p for p in self._explicit_paths if p.exists()]

        from lore.extractors.factory import get_all_extractors

        extractors = [ex for ex in get_all_extractors() if ex.is_available()]
        return collect_extractor_paths(extractors)

    @staticmethod
    def _max_mtime(path: Path) -> float:
        """Return the most recent mtime under ``path``.

        For files we use their mtime directly. For directories we walk one
        level deep on the top dir plus recursively scan for files (capped at
        a reasonable depth via ``rglob('*')``). Errors are swallowed so
        permission issues on a single subdir don't poison the snapshot.
        """

        try:
            if path.is_file():
                return path.stat().st_mtime
        except OSError:
            return 0.0

        latest = 0.0
        try:
            latest = path.stat().st_mtime
        except OSError:
            pass

        try:
            for child in path.rglob("*"):
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    continue
                if mtime > latest:
                    latest = mtime
        except OSError:
            pass
        return latest

    def _snapshot(self) -> dict[Path, float]:
        return {p: self._max_mtime(p) for p in self._get_watch_paths()}

    def _check_changes(self) -> list[Path]:
        """Return the paths whose mtime changed since the last snapshot."""
        current = self._snapshot()
        changed: list[Path] = []
        for path, mtime in current.items():
            previous = self._last_mtimes.get(path)
            if previous is None or mtime > previous:
                changed.append(path)
        self._last_mtimes = current
        return changed

    def _run(self) -> None:
        # Establish a baseline so we don't fire immediately on startup.
        self._last_mtimes = self._snapshot()
        logger.info(
            "SessionWatcher started: watching %d path(s), interval=%.1fs",
            len(self._last_mtimes),
            self._interval,
        )
        while not self._stop.is_set():
            if self._stop.wait(self._interval):
                break
            try:
                changed = self._check_changes()
            except Exception:  # noqa: BLE001 — keep watcher alive
                logger.exception("SessionWatcher snapshot failed")
                continue
            if not changed:
                continue
            logger.info("SessionWatcher detected changes in %d path(s)", len(changed))
            try:
                self._callback()
            except Exception:  # noqa: BLE001 — callback must not kill watcher
                logger.exception("SessionWatcher callback raised")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="lore-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> list[Path]:
        """Run a single change-detection pass.

        Useful for the CLI watch loop where we want synchronous behaviour
        and full control over the sleep cadence.
        """

        if not self._last_mtimes:
            self._last_mtimes = self._snapshot()
            return []
        return self._check_changes()
