"""Tests for ``lore worker`` (# B3)."""

from __future__ import annotations

import json
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.services.worker import (
    load_status,
    read_store_stamp,
    run_loop,
    run_pass,
    status_check,
    worker_dir,
)
from lore.storage.freshness import GENERATED_AT_KEY
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path


def _session(sid: str = "s1") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path="/p",
        title="t",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="hello " * 200,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="world " * 200,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ],
    )


def _stamp_store(tmp_path: Path, stamp: str) -> None:
    IndexBuilder(tmp_path).build_index([_session()], {})
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO store_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (GENERATED_AT_KEY, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _job_recorders():
    calls: list[str] = []

    def library(_output_dir: Path) -> dict:
        calls.append("library")
        return {"status": "ok", "skipped": False}

    def embed(_output_dir: Path) -> dict:
        calls.append("embed")
        return {"status": "ok", "skipped": False}

    def summaries(_output_dir: Path, **_kwargs) -> dict:
        calls.append("summaries")
        return {"status": "ok", "skipped": False, "written": 1}

    return calls, {"library": library, "embed_messages": embed, "summaries": summaries}


def test_noop_when_stamp_unchanged(tmp_path: Path):
    stamp = "2026-11-10T10:00:00+00:00"
    _stamp_store(tmp_path, stamp)
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    (wdir / "state.json").write_text(
        json.dumps({"last_stamp": stamp, "jobs": {}}), encoding="utf-8"
    )
    calls, jobs = _job_recorders()
    changed = run_pass(tmp_path, jobs=jobs)
    assert changed is False
    assert calls == []


def test_runs_jobs_when_stamp_changes_and_records_state(tmp_path: Path):
    _stamp_store(tmp_path, "2026-11-10T10:00:00+00:00")
    calls, jobs = _job_recorders()
    assert run_pass(tmp_path, jobs=jobs) is True
    assert calls == ["library", "embed", "summaries"]
    state = json.loads((worker_dir(tmp_path) / "state.json").read_text(encoding="utf-8"))
    assert state["last_stamp"] == "2026-11-10T10:00:00+00:00"
    assert state["jobs"]["library"]["status"] == "ok"
    assert (worker_dir(tmp_path) / "heartbeat").is_file()


def test_failing_job_recorded_others_still_run(tmp_path: Path):
    _stamp_store(tmp_path, "2026-11-10T11:00:00+00:00")
    calls: list[str] = []

    def library(_output_dir: Path) -> dict:
        calls.append("library")
        raise RuntimeError("boom")

    def embed(_output_dir: Path) -> dict:
        calls.append("embed")
        return {"status": "ok", "skipped": False}

    def summaries(_output_dir: Path, **_kwargs) -> dict:
        calls.append("summaries")
        return {"status": "ok", "skipped": False}

    jobs = {"library": library, "embed_messages": embed, "summaries": summaries}
    assert run_pass(tmp_path, jobs=jobs) is True
    assert calls == ["library", "embed", "summaries"]
    state = json.loads((worker_dir(tmp_path) / "state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["library"]["status"] == "error"
    assert "boom" in (state["jobs"]["library"].get("last_error") or "")
    assert state["jobs"]["embed_messages"]["status"] == "ok"
    assert state.get("last_stamp") is None


def test_failing_job_does_not_advance_stamp_when_partial_ok(tmp_path: Path):
    _stamp_store(tmp_path, "2026-11-10T11:05:00+00:00")
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    (wdir / "state.json").write_text(
        json.dumps({"last_stamp": "2026-11-10T10:00:00+00:00", "jobs": {}}),
        encoding="utf-8",
    )

    def library(_output_dir: Path) -> dict:
        return {"status": "error", "skipped": False, "last_error": "nope"}

    jobs = {
        "library": library,
        "embed_messages": lambda _o: {"status": "ok", "skipped": False},
        "summaries": lambda _o, **_k: {"status": "ok", "skipped": False},
    }
    run_pass(tmp_path, jobs=jobs)
    state = json.loads((wdir / "state.json").read_text(encoding="utf-8"))
    assert state["last_stamp"] == "2026-11-10T10:00:00+00:00"
    assert state["jobs"]["library"].get("ok_stamp") != "2026-11-10T11:05:00+00:00"


def test_failed_job_retries_after_backoff_without_new_stamp(tmp_path: Path, monkeypatch):
    stamp = "2026-11-10T11:10:00+00:00"
    _stamp_store(tmp_path, stamp)
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    past = (datetime.now(timezone.utc).timestamp()) - 10
    past_iso = datetime.fromtimestamp(past, timezone.utc).isoformat(timespec="seconds")
    (wdir / "state.json").write_text(
        json.dumps(
            {
                "last_stamp": "2026-11-10T10:00:00+00:00",
                "jobs": {
                    "library": {
                        "status": "error",
                        "retry_after": past_iso,
                        "ok_stamp": None,
                    },
                    "embed_messages": {"status": "ok", "ok_stamp": stamp},
                    "summaries": {"status": "ok", "ok_stamp": stamp},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LORE_WORKER_INTERVAL", "60")

    calls: list[str] = []

    def library(_output_dir: Path) -> dict:
        calls.append("library")
        return {"status": "ok", "skipped": False}

    def _fail_if_called(_o, *_a, **_k):
        raise AssertionError("unexpected job")

    jobs = {
        "library": library,
        "embed_messages": _fail_if_called,
        "summaries": _fail_if_called,
    }
    assert run_pass(tmp_path, jobs=jobs) is True
    assert calls == ["library"]
    state = json.loads((wdir / "state.json").read_text(encoding="utf-8"))
    assert state["last_stamp"] == stamp


def test_missing_library_command_skipped_not_failed(tmp_path: Path, monkeypatch):
    _stamp_store(tmp_path, "2026-11-10T12:00:00+00:00")
    monkeypatch.delitem(lore_cli.COMMANDS, "library", raising=False)

    calls: list[str] = []

    def embed(_output_dir: Path) -> dict:
        calls.append("embed")
        return {"status": "ok", "skipped": False}

    def summaries(_output_dir: Path, **_kwargs) -> dict:
        return {"status": "ok", "skipped": False}

    from lore.services import worker as worker_mod

    run_pass(
        tmp_path,
        jobs={
            "library": worker_mod.run_library_job,
            "embed_messages": embed,
            "summaries": summaries,
        },
    )
    state = json.loads((worker_dir(tmp_path) / "state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["library"]["status"] == "skipped"
    assert calls == ["embed"]


def test_summary_cap_respected(tmp_path: Path, monkeypatch):
    _stamp_store(tmp_path, "2026-11-10T13:00:00+00:00")
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date().isoformat()
    (wdir / "state.json").write_text(
        json.dumps({"summary_daily": {"date": today, "count": 19}, "jobs": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LORE_WORKER_SUMMARY_DAILY_CAP", "20")

    written = {"n": 0}

    def summaries(_output_dir: Path, *, remaining_cap: int, **_kwargs) -> dict:
        assert remaining_cap == 1
        written["n"] += 1
        return {"status": "ok", "skipped": False, "written": 1}

    jobs = {
        "library": lambda _o: {"status": "skipped", "skipped": True},
        "embed_messages": lambda _o: {"status": "skipped", "skipped": True},
        "summaries": summaries,
    }
    run_pass(tmp_path, jobs=jobs)
    assert written["n"] == 1
    state = json.loads((wdir / "state.json").read_text(encoding="utf-8"))
    assert state["summary_daily"]["count"] == 20

    written["n"] = 0
    _stamp_store(tmp_path, "2026-11-10T13:30:00+00:00")
    run_pass(tmp_path, jobs=jobs)
    assert written["n"] == 0


def test_summaries_skip_cloud_without_opt_in(tmp_path: Path, monkeypatch):
    _stamp_store(tmp_path, "2026-11-10T14:00:00+00:00")
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "gemini")
    from lore.services import worker as worker_mod

    result = worker_mod.run_summaries_job(tmp_path, daily_cap=20, remaining_cap=5)
    assert result["skipped"] is True
    assert result["status"] == "skipped"


def test_status_check_fails_on_stale_heartbeat(tmp_path: Path):
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    hb = wdir / "heartbeat"
    old = time.time() - 1000
    Path(hb).write_text("", encoding="utf-8")
    import os

    os.utime(hb, (old, old))
    assert status_check(tmp_path, interval=100) == 1


def test_run_loop_stops_on_stop_event(tmp_path: Path):
    _stamp_store(tmp_path, "2026-11-10T15:00:00+00:00")
    stop = threading.Event()
    iterations = {"n": 0}

    def on_tick(_output_dir: Path) -> None:
        iterations["n"] += 1
        stop.set()

    run_loop(tmp_path, interval=0.05, stop_event=stop, on_tick=on_tick)
    assert iterations["n"] >= 1


def test_run_loop_without_stop_event_stops_via_sig_handler(tmp_path: Path, monkeypatch):
    from lore.services import worker as worker_mod

    _stamp_store(tmp_path, "2026-11-10T15:01:00+00:00")
    holder: dict[str, object] = {}
    real_signal = worker_mod.signal.signal

    def capture(sig, handler):
        if sig == signal.SIGINT:
            holder["handler"] = handler
        if threading.current_thread() is threading.main_thread():
            return real_signal(sig, handler)
        return signal.SIG_DFL

    monkeypatch.setattr(worker_mod.signal, "signal", capture)
    thread = threading.Thread(
        target=run_loop,
        kwargs={"output_dir": tmp_path, "interval": 0.2},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 2.0
    while "handler" not in holder and time.time() < deadline:
        time.sleep(0.02)
    assert "handler" in holder
    holder["handler"](signal.SIGINT, None)  # type: ignore[operator]
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_run_loop_default_sig_handler_sets_stop(tmp_path: Path, monkeypatch):
    from lore.services import worker as worker_mod

    stopping = threading.Event()
    installed: dict[int, object] = {}
    real_signal = worker_mod.signal.signal

    def capture(sig, handler):
        installed[sig] = handler
        if threading.current_thread() is threading.main_thread():
            return real_signal(sig, handler)
        return signal.SIG_DFL

    monkeypatch.setattr(worker_mod.signal, "signal", capture)
    t = threading.Thread(
        target=run_loop,
        kwargs={"output_dir": tmp_path, "interval": 5.0, "stop_event": stopping},
        daemon=True,
    )
    t.start()
    time.sleep(0.05)
    assert signal.SIGINT in installed
    installed[signal.SIGINT](signal.SIGINT, None)
    assert stopping.is_set()
    stopping.set()
    t.join(timeout=2.0)


def test_corrupt_state_json_warns_and_treated_empty(tmp_path: Path, capsys):
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    (wdir / "state.json").write_text("{not json", encoding="utf-8")
    assert load_status(tmp_path)["jobs"] == {}
    err = capsys.readouterr().err
    assert "state" in err.lower() or "worker" in err.lower()


def test_status_check_survives_corrupt_state(tmp_path: Path):
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    (wdir / "state.json").write_text("[]", encoding="utf-8")
    (wdir / "heartbeat").write_text("x", encoding="utf-8")
    assert status_check(tmp_path, interval=300) == 0


def test_read_store_stamp(tmp_path: Path):
    assert read_store_stamp(tmp_path) is None
    _stamp_store(tmp_path, "2026-11-10T16:00:00+00:00")
    assert read_store_stamp(tmp_path) == "2026-11-10T16:00:00+00:00"


def test_worker_status_json_read_only(tmp_path: Path, capsys):
    wdir = worker_dir(tmp_path)
    wdir.mkdir(parents=True)
    payload = {"last_stamp": "x", "jobs": {}}
    (wdir / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    (wdir / "heartbeat").write_text("", encoding="utf-8")
    before = {p.name: p.stat().st_mtime_ns for p in wdir.iterdir()}

    parser = lore_cli.build_parser()
    args = parser.parse_args(["--output-dir", str(tmp_path), "worker", "status", "--json"])
    assert lore_cli.cmd_worker(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["last_stamp"] == "x"
    after = {p.name: p.stat().st_mtime_ns for p in wdir.iterdir()}
    assert before == after
