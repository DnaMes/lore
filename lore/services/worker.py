"""Background worker: library export, embeddings, and summaries after each sync stamp."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from lore.storage.embeddings import embeddings_available, sqlite_vec_available
from lore.storage.reader import _read_generated_at
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300
DEFAULT_SUMMARY_CAP = 20
LIBRARY_TIMEOUT = 1800
EMBED_TIMEOUT = 600
SUMMARY_TIMEOUT = 1800

JobFn = Callable[..., dict[str, Any]]


def worker_dir(output_dir: Path) -> Path:
    return output_dir / "worker"


def read_store_stamp(output_dir: Path) -> Optional[str]:
    db = v2_db_path(output_dir)
    if not db.exists():
        return None
    conn = open_connection(db)
    try:
        return _read_generated_at(conn)
    finally:
        conn.close()


def _default_jobs() -> dict[str, JobFn]:
    return {
        "library": run_library_job,
        "embed_messages": run_embed_messages_job,
        "summaries": run_summaries_job,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def touch_heartbeat(output_dir: Path) -> None:
    hb = worker_dir(output_dir) / "heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(_now_iso() + "\n", encoding="utf-8")


def heartbeat_mtime(output_dir: Path) -> Optional[float]:
    hb = worker_dir(output_dir) / "heartbeat"
    if not hb.is_file():
        return None
    try:
        return hb.stat().st_mtime
    except OSError:
        return None


def resolve_interval() -> int:
    raw = os.environ.get("LORE_WORKER_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_INTERVAL
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INTERVAL
    return max(1, value)


def resolve_summary_cap() -> int:
    raw = os.environ.get("LORE_WORKER_SUMMARY_DAILY_CAP", "").strip()
    if not raw:
        return DEFAULT_SUMMARY_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_SUMMARY_CAP


def summary_remaining(state: dict[str, Any], cap: int) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    entry = state.get("summary_daily") or {}
    if entry.get("date") != today:
        return cap
    return max(0, cap - int(entry.get("count") or 0))


def _record_job(
    state: dict[str, Any],
    name: str,
    result: dict[str, Any],
    *,
    error: Optional[str] = None,
) -> None:
    jobs = state.setdefault("jobs", {})
    entry = {
        "last_run": _now_iso(),
        "status": result.get("status", "ok"),
        "skipped": bool(result.get("skipped")),
        "last_error": error or result.get("last_error"),
    }
    if "written" in result:
        entry["written"] = result["written"]
    if "embedded" in result:
        entry["embedded"] = result["embedded"]
    jobs[name] = entry


def _run_job_with_timeout(fn: Callable[[], dict[str, Any]], timeout: int) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeoutError:
            return {
                "status": "error",
                "skipped": False,
                "last_error": f"timed out after {timeout}s",
            }
        except Exception as exc:  # noqa: BLE001 — isolate per-job failures
            return {"status": "error", "skipped": False, "last_error": str(exc)}


def run_library_job(output_dir: Path) -> dict[str, Any]:
    import lore_cli

    if "library" not in lore_cli.COMMANDS:
        print("worker: skipping library (command not available)", file=sys.stderr)
        return {"status": "skipped", "skipped": True}

    cli_path = Path(lore_cli.__file__).resolve()
    cmd = [
        sys.executable,
        str(cli_path),
        "library",
        "--output-dir",
        str(output_dir),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=LIBRARY_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "skipped": False, "last_error": "library subprocess timed out"}
    except OSError as exc:
        return {"status": "error", "skipped": False, "last_error": str(exc)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        print(f"worker: library failed: {err}", file=sys.stderr)
        return {"status": "error", "skipped": False, "last_error": err}
    return {"status": "ok", "skipped": False}


def run_embed_messages_job(output_dir: Path) -> dict[str, Any]:
    from lore.storage import embed_messages

    if not (embeddings_available() and sqlite_vec_available()):
        print(
            "worker: skipping embed-messages (semantic extra not installed)",
            file=sys.stderr,
        )
        return {"status": "skipped", "skipped": True}
    db = v2_db_path(output_dir)
    if not db.exists():
        return {"status": "skipped", "skipped": True, "last_error": "no v2 store"}

    def _run() -> dict[str, Any]:
        conn = open_connection(db)
        try:
            written = embed_messages(conn, budget_seconds=float(EMBED_TIMEOUT))
        finally:
            conn.close()
        return {"status": "ok", "skipped": False, "embedded": written}

    return _run_job_with_timeout(_run, EMBED_TIMEOUT)


def run_summaries_job(
    output_dir: Path,
    *,
    daily_cap: Optional[int] = None,
    remaining_cap: Optional[int] = None,
    allow_cloud: bool = False,
) -> dict[str, Any]:
    from lore.llm import LLMConfig, get_provider
    from lore.services.summarize import run_summaries, select_candidates
    from lore.services.summarize_cli import (
        cloud_opt_in_granted,
        default_model,
        provider_locality,
        require_cloud_opt_in,
    )

    cap = daily_cap if daily_cap is not None else resolve_summary_cap()
    if remaining_cap is not None and remaining_cap <= 0:
        print("worker: skipping summaries (daily cap reached)", file=sys.stderr)
        return {"status": "skipped", "skipped": True}

    provider_name = os.environ.get("LORE_SUMMARY_PROVIDER", "ollama").strip().lower() or "ollama"
    if require_cloud_opt_in(provider_name, allow_cloud) == 2:
        return {"status": "skipped", "skipped": True}
    if provider_locality(provider_name) != "local" and not cloud_opt_in_granted(allow_cloud):
        print(
            f"worker: skipping summaries (provider {provider_name!r} needs cloud opt-in)",
            file=sys.stderr,
        )
        return {"status": "skipped", "skipped": True}

    limit = remaining_cap if remaining_cap is not None else cap
    if limit <= 0:
        return {"status": "skipped", "skipped": True}

    candidates = select_candidates(output_dir, min_idle_minutes=30, limit=limit)
    if not candidates:
        return {"status": "ok", "skipped": False, "written": 0}

    model = default_model(provider_name)
    model_label = f"{provider_name}/{model}"

    def _run() -> dict[str, Any]:
        config = LLMConfig.for_transcripts(provider=provider_name, model=model)
        provider = get_provider(
            provider=config.provider,
            model=config.model,
            cache_enabled=config.cache_enabled,
            options=config.options,
            num_ctx=config.num_ctx,
            allow_cloud=allow_cloud,
        )
        if not provider.is_available():
            print(f"worker: skipping summaries ({provider_name} not available)", file=sys.stderr)
            return {"status": "skipped", "skipped": True}
        report = run_summaries(
            output_dir,
            candidates,
            provider,
            model_label,
            max_sessions=limit,
        )
        return {"status": "ok", "skipped": False, "written": report.written}

    return _run_job_with_timeout(_run, SUMMARY_TIMEOUT)


def run_pass(
    output_dir: Path,
    *,
    jobs: Optional[dict[str, JobFn]] = None,
) -> bool:
    """Run worker jobs when the store stamp changed. Returns True when jobs ran."""
    touch_heartbeat(output_dir)
    stamp = read_store_stamp(output_dir)
    if not stamp:
        return False

    state_path = worker_dir(output_dir) / "state.json"
    state = _load_state(state_path)
    if stamp == state.get("last_stamp"):
        return False

    job_map = jobs or _default_jobs()
    cap = resolve_summary_cap()
    remaining = summary_remaining(state, cap)
    allow_cloud = False  # ponytail: worker never opts into cloud by itself

    order = ("library", "embed_messages", "summaries")
    for name in order:
        fn = job_map.get(name)
        if fn is None:
            continue
        try:
            if name == "summaries":
                result = fn(
                    output_dir,
                    daily_cap=cap,
                    remaining_cap=remaining,
                    allow_cloud=allow_cloud,
                )
                if int(result.get("written") or 0) > 0:
                    today = datetime.now(timezone.utc).date().isoformat()
                    daily = state.setdefault("summary_daily", {})
                    if daily.get("date") != today:
                        daily = {"date": today, "count": 0}
                        state["summary_daily"] = daily
                    daily["count"] = int(daily.get("count") or 0) + int(result["written"])
                    remaining = max(0, remaining - int(result["written"]))
            else:
                result = fn(output_dir)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "error", "skipped": False, "last_error": str(exc)}
            print(f"worker: {name} failed: {exc}", file=sys.stderr)
        _record_job(state, name, result, error=result.get("last_error"))

    state["last_stamp"] = stamp
    state["last_pass_at"] = _now_iso()
    _save_state(state_path, state)
    return True


def run_loop(
    output_dir: Path,
    *,
    interval: Optional[float] = None,
    stop_event: Optional[Any] = None,
    on_tick: Optional[Callable[[Path], None]] = None,
) -> None:
    """Main worker loop until ``stop_event`` is set or SIGINT/SIGTERM."""
    pause = float(interval if interval is not None else resolve_interval())
    stopping = stop_event

    def _handle_sig(_signum, _frame) -> None:
        if stopping is not None:
            stopping.set()

    old_int = signal.signal(signal.SIGINT, _handle_sig)
    old_term = signal.signal(signal.SIGTERM, _handle_sig)
    try:
        while stopping is None or not stopping.is_set():
            touch_heartbeat(output_dir)
            run_pass(output_dir)
            if on_tick is not None:
                on_tick(output_dir)
            if stopping is not None and stopping.wait(pause):
                break
            if stopping is None:
                time.sleep(pause)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def load_status(output_dir: Path) -> dict[str, Any]:
    state = _load_state(worker_dir(output_dir) / "state.json")
    state.setdefault("jobs", {})
    mtime = heartbeat_mtime(output_dir)
    state["heartbeat_mtime"] = mtime
    state["interval_seconds"] = resolve_interval()
    state["store_stamp"] = read_store_stamp(output_dir)
    return state


def status_check(output_dir: Path, *, interval: Optional[int] = None) -> int:
    pause = interval if interval is not None else resolve_interval()
    mtime = heartbeat_mtime(output_dir)
    if mtime is None:
        return 1
    age = time.time() - mtime
    if age > pause * 3:
        return 1
    return 0
