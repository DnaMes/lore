"""Scratch-copy SQLite helpers shared by Goose and Crush extractors."""

from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Set

from ..utils.datetime import make_naive, parse_timestamp

MAX_SQLITE_BYTES = 512 * 1024 * 1024
MAX_SESSIONS_PER_DB = 5000
MAX_MESSAGES_PER_SESSION = 5000
MAX_CONTENT_CHARS = 100_000
MAX_REASONING_CHARS = 20_000
MAX_TOOL_ARG_CHARS = 200
MAX_TOOL_ARGS = 50
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_DATA_BYTES = 4 * 1024 * 1024
MAX_FETCH_BYTES = 128 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_TITLE_CHARS = 200
SQLITE_TIMEOUT = 2.0
QUERY_SECONDS = 10.0


def table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


def open_readonly_copy(copy_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(
            f"file:{copy_path}?mode=ro",
            uri=True,
            timeout=SQLITE_TIMEOUT,
        )
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + QUERY_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        return conn
    except sqlite3.Error:
        return None


def is_plain_db(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def clean_absolute_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_PATH_CHARS:
        return None
    if any(c in value for c in "\x00\r\n") or ".." in Path(value).parts:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return value


def cap_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def safe_ts_any(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                return fallback
            if value > 1_000_000_000_000:
                value = value / 1000.0
            return make_naive(parse_timestamp(value))
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError, RecursionError):
        return fallback


def select_column(columns: Set[str], name: str, *, large_json: bool = False) -> str:
    if name not in columns:
        return f"NULL AS {name}"
    if large_json or name in ("content_json", "parts", "metadata_json", "model_config_json"):
        return (
            f"CASE WHEN length({name}) > {MAX_DATA_BYTES} THEN NULL "
            f"ELSE substr({name}, 1, {MAX_DATA_BYTES}) END AS {name}"
        )
    if name in ("role", "name", "session_type", "title"):
        return f"substr({name}, 1, 512) AS {name}"
    return name


def render_tool_arguments(
    arguments: Any,
    is_secret: Callable[[str], bool],
) -> str:
    rendered: list[str] = []
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if len(rendered) >= MAX_TOOL_ARGS:
                break
            if not isinstance(key, str) or is_secret(key):
                continue
            key = key[:100]
            if isinstance(value, dict):
                rendered.append(f"{key}=<dict>")
                continue
            if isinstance(value, list):
                rendered.append(f"{key}=<list>")
                continue
            v_str = str(value)
            if len(v_str) > MAX_TOOL_ARG_CHARS:
                v_str = v_str[:MAX_TOOL_ARG_CHARS]
            rendered.append(f"{key}={v_str}")
    return (" " + " ".join(rendered)) if rendered else ""


def tool_result_status(status: Any, is_error: Any) -> tuple[str, bool]:
    if status == "error" or is_error in (True, 1):
        return "failed", True
    return "completed", False
