"""Background worker: library export, embeddings, and summaries after each sync stamp."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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
MIN_JOB_RETRY_SECONDS = 900
DEFAULT_SUMMARY_CAP = 20
LIBRARY_TIMEOUT = 1800
HEARTBEAT_POLL_SECONDS = 15
# Seconds of embedding per pass (child process); more passes drain a backlog.
DEFAULT_EMBED_BUDGET = 120
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
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"worker: ignoring unreadable state {path}: {exc}", file=sys.stderr)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"worker: ignoring corrupt state {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"worker: ignoring corrupt state {path}: expected object", file=sys.stderr)
        return {}
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
    if result.get("timed_out"):
        entry["timed_out"] = True
    if "written" in result:
        entry["written"] = result["written"]
    if "embedded" in result:
        entry["embedded"] = result["embedded"]
    jobs[name] = entry


def _job_retry_interval() -> int:
    return max(resolve_interval(), MIN_JOB_RETRY_SECONDS)


def _parse_iso_ts(value: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _job_retry_due(state: dict[str, Any], name: str) -> bool:
    entry = (state.get("jobs") or {}).get(name) or {}
    if entry.get("pending"):
        return True
    if entry.get("status") != "error":
        return False
    retry_after = entry.get("retry_after")
    if not retry_after:
        return True
    due = _parse_iso_ts(str(retry_after))
    if due is None:
        return True
    return time.time() >= due


def _job_backing_off(state: dict[str, Any], name: str) -> bool:
    entry = (state.get("jobs") or {}).get(name) or {}
    if entry.get("status") != "error" or not entry.get("retry_after"):
        return False
    due = _parse_iso_ts(str(entry["retry_after"]))
    return due is not None and time.time() < due


def _job_done_for_stamp(state: dict[str, Any], name: str, stamp: str) -> bool:
    entry = (state.get("jobs") or {}).get(name) or {}
    if entry.get("ok_stamp") == stamp:
        return True
    if state.get("last_stamp") == stamp:
        status = entry.get("status")
        if status in ("ok", "skipped"):
            return True
    return False


def _stamp_fully_processed(state: dict[str, Any], stamp: str, names: tuple[str, ...]) -> bool:
    for name in names:
        if not _job_done_for_stamp(state, name, stamp):
            return False
    return True


def _run_job_with_timeout(
    fn: Callable[[], dict[str, Any]],
    timeout: int,
    *,
    output_dir: Optional[Path] = None,
) -> dict[str, Any]:
    tick = float(resolve_interval())
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(fn)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # A hung job must not block here: do not wait for it on exit.
                pool.shutdown(wait=False, cancel_futures=True)
                return {
                    "status": "error",
                    "skipped": False,
                    "timed_out": True,
                    "last_error": f"timed out after {timeout}s",
                }
            wait = min(tick, max(0.05, remaining))
            try:
                return fut.result(timeout=wait)
            except FuturesTimeoutError:
                if output_dir is not None:
                    touch_heartbeat(output_dir)
            except Exception as exc:  # noqa: BLE001 — isolate per-job failures
                return {"status": "error", "skipped": False, "last_error": str(exc)}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _lore_command_head() -> list[str]:
    import lore_cli

    console = shutil.which("lore")
    if console:
        return [console]
    return [sys.executable, str(Path(lore_cli.__file__).resolve())]


def _run_cli_child(
    output_dir: Path, args: list[str], timeout: float, label: str
) -> tuple[dict[str, Any], str]:
    """Run ``lore --output-dir <dir> <args>`` as a child, keeping the heartbeat fresh.

    A child process returns its memory when it exits; the loop process stays small.
    Returns the job result and the child's stdout.
    """
    # --output-dir is a global option and must precede the subcommand.
    cmd = [*_lore_command_head(), "--output-dir", str(output_dir), *args]
    deadline = time.monotonic() + timeout
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out_f,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err_f,
    ):
        try:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, text=True)
        except OSError as exc:
            return {"status": "error", "skipped": False, "last_error": str(exc)}, ""
        # Poll instead of blocking so the heartbeat stays fresh during long runs.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                return {
                    "status": "error",
                    "skipped": False,
                    "timed_out": True,
                    "last_error": f"{label} subprocess timed out",
                }, ""
            try:
                proc.wait(timeout=min(HEARTBEAT_POLL_SECONDS, remaining))
                break
            except subprocess.TimeoutExpired:
                touch_heartbeat(output_dir)
        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read()
        stderr = err_f.read()

    if proc.returncode != 0:
        err = (stderr or stdout or "").strip() or f"exit {proc.returncode}"
        print(f"worker: {label} failed: {err}", file=sys.stderr)
        return {"status": "error", "skipped": False, "last_error": err}, stdout
    return {"status": "ok", "skipped": False}, stdout


def run_library_job(output_dir: Path) -> dict[str, Any]:
    import lore_cli

    if "library" not in lore_cli.COMMANDS:
        print("worker: skipping library (command not available)", file=sys.stderr)
        return {"status": "skipped", "skipped": True}
    result, _stdout = _run_cli_child(output_dir, ["library"], LIBRARY_TIMEOUT, "library")
    return result


def _embed_budget() -> int:
    raw = os.environ.get("LORE_WORKER_EMBED_BUDGET", "").strip()
    try:
        return max(10, int(raw)) if raw else DEFAULT_EMBED_BUDGET
    except ValueError:
        return DEFAULT_EMBED_BUDGET


def run_embed_messages_job(output_dir: Path) -> dict[str, Any]:
    if not (embeddings_available() and sqlite_vec_available()):
        print(
            "worker: skipping embed-messages (semantic extra not installed)",
            file=sys.stderr,
        )
        return {"status": "skipped", "skipped": True}
    db = v2_db_path(output_dir)
    if not db.exists():
        return {"status": "skipped", "skipped": True, "last_error": "no v2 store"}
    budget = _embed_budget()
    # embed-messages commits its progress, so a budgeted run continues next pass.
    result, stdout = _run_cli_child(
        output_dir, ["embed-messages", "--budget", str(budget)], budget + 120, "embed-messages"
    )
    if result.get("status") == "ok":
        match = re.search(r"Embedded (\d+) message", stdout or "")
        embedded = int(match.group(1)) if match else 0
        result["embedded"] = embedded
        result["more"] = embedded > 0  # a run that did work may have stopped on its budget
    return result


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
        return {"status": "skipped", "skipped": True, "deferred_daily_cap": True}

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
        return {"status": "skipped", "skipped": True, "deferred_daily_cap": True}

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

    return _run_job_with_timeout(_run, SUMMARY_TIMEOUT, output_dir=output_dir)


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
    job_map = jobs or _default_jobs()
    cap = resolve_summary_cap()
    remaining = summary_remaining(state, cap)
    allow_cloud = False  # ponytail: worker never opts into cloud by itself

    order = ("library", "embed_messages", "summaries")
    stamp_changed = stamp != state.get("last_stamp")
    jobs_to_run: list[str] = []
    for name in order:
        if job_map.get(name) is None:
            continue
        if _job_done_for_stamp(state, name, stamp):
            continue
        # last_stamp only advances once every job is done, so "changed" stays true while a
        # job keeps failing; its backoff must still hold.
        if _job_backing_off(state, name):
            continue
        if stamp_changed or _job_retry_due(state, name):
            jobs_to_run.append(name)

    if not jobs_to_run:
        return False

    retry_pause = _job_retry_interval()
    for name in jobs_to_run:
        fn = job_map.get(name)
        if fn is None:
            continue
        touch_heartbeat(output_dir)
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
        entry = state.setdefault("jobs", {}).setdefault(name, {})
        if result.get("status") == "error":
            retry_at = datetime.fromtimestamp(
                time.time() + retry_pause,
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
            entry["retry_after"] = retry_at
            entry.pop("pending", None)  # a failure backs off, even after a pending run
        elif result.get("more"):
            # Work left for this stamp: run again next pass without waiting for a sync.
            entry["pending"] = True
            entry.pop("retry_after", None)
        elif result.get("status") == "ok" or (
            result.get("skipped") and not result.get("deferred_daily_cap")
        ):
            entry["ok_stamp"] = stamp
            entry.pop("retry_after", None)
            entry.pop("pending", None)

    if _stamp_fully_processed(state, stamp, order):
        state["last_stamp"] = stamp
    state["last_pass_at"] = _now_iso()
    _save_state(state_path, state)
    return True


def run_loop(
    output_dir: Path,
    *,
    interval: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
    on_tick: Optional[Callable[[Path], None]] = None,
) -> None:
    """Main worker loop until ``stop_event`` is set or SIGINT/SIGTERM."""
    pause = float(interval if interval is not None else resolve_interval())
    stopping = stop_event if stop_event is not None else threading.Event()

    def _handle_sig(_signum, _frame) -> None:
        stopping.set()

    # signal.signal only works on the main thread; elsewhere rely on the event.
    on_main = threading.current_thread() is threading.main_thread()
    old_int = old_term = None
    if on_main:
        old_int = signal.signal(signal.SIGINT, _handle_sig)
        old_term = signal.signal(signal.SIGTERM, _handle_sig)
    try:
        while not stopping.is_set():
            touch_heartbeat(output_dir)
            try:
                run_pass(output_dir)
            except Exception as exc:  # noqa: BLE001 — one bad pass must not end the loop
                _record_loop_error(output_dir, exc)
            if on_tick is not None:
                on_tick(output_dir)
            if stopping.wait(pause):
                break
    finally:
        if on_main:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


def _record_loop_error(output_dir: Path, exc: Exception) -> None:
    msg = str(exc) or type(exc).__name__
    print(f"worker: pass failed: {msg}", file=sys.stderr)
    try:
        state_path = worker_dir(output_dir) / "state.json"
        state = _load_state(state_path)
        state["last_error"] = {"at": _now_iso(), "error": msg}
        _save_state(state_path, state)
    except OSError as save_exc:
        print(f"worker: could not record error: {save_exc}", file=sys.stderr)


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
