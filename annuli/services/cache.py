"""Thread-safe LRU cache decorator.

A framework-free utility used by the service layer and the web
interface. Lives here (not in ``web_data``) so the non-interface
service modules can use it without importing an interface module.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any, Callable


def threadsafe_lru_cache(maxsize: int = 128) -> Callable:
    """Thread-safe LRU cache decorator for use in multi-threaded apps."""

    def decorator(func: Callable) -> Any:
        cached_func = lru_cache(maxsize=maxsize)(func)
        cache_lock = threading.Lock()

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with cache_lock:
                return cached_func(*args, **kwargs)

        wrapper.cache_clear = cached_func.cache_clear  # type: ignore[attr-defined]
        wrapper.cache_info = cached_func.cache_info  # type: ignore[attr-defined]
        return wrapper

    return decorator
