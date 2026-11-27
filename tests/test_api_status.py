"""Tests for GET /api/status."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from annuli import __version__
from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.interfaces import web
from annuli.storage import v2_db_path, write_sessions


def _make_session(sid: str = "s-status-1") -> UnifiedSession:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    return UnifiedSession(
        session_id=sid,
        tool=Tool.CLAUDE_CODE,
        title="Status check session",
        created_at=now,
        last_updated=now,
        project_path="/repo/demo",
        messages=[
            UnifiedMessage(role=Role.USER, content="hello", timestamp=now),
            UnifiedMessage(role=Role.ASSISTANT, content="hi there", timestamp=now),
        ],
    )


@pytest.fixture()
def status_client(tmp_path: Path, monkeypatch):
    """Flask test client backed by a fresh temporary store."""
    output_dir = tmp_path / "annuli-out"
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.json"

    write_sessions(v2_db_path(output_dir), [_make_session()])
    index_path.write_text(json.dumps({"sessions": [], "stats": {}}), encoding="utf-8")

    monkeypatch.setattr(web, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "DELETED_SESSIONS_PATH", output_dir / "deleted_sessions.json")

    with web.app.test_client() as client:
        yield client, output_dir


def test_api_status_returns_200_and_all_documented_keys(status_client):
    client, _ = status_client
    resp = client.get("/api/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload is not None

    for key in (
        "sessions",
        "index",
        "search",
        "worker",
        "llm",
        "version",
        "revision",
        "generated_at",
        "errors",
    ):
        assert key in payload, f"missing key: {key}"

    assert payload["errors"] == []
    assert payload["version"] == __version__
    assert isinstance(payload["revision"], str)
    assert payload["generated_at"].endswith("Z")

    assert set(payload["sessions"].keys()) == {
        "total_sessions",
        "automated_hidden",
        "last_updated",
    }
    assert isinstance(payload["sessions"]["total_sessions"], int)
    assert payload["sessions"]["automated_hidden"] == 0

    assert set(payload["index"].keys()) == {"fresh", "reasons"}
    assert isinstance(payload["index"]["fresh"], bool)
    assert isinstance(payload["index"]["reasons"], list)

    assert set(payload["search"].keys()) == {"mode", "message_index"}
    assert payload["search"]["mode"] in {"hybrid", "keyword"}
    assert payload["search"]["message_index"] in {"ready", "backfilling", "absent"}

    assert set(payload["worker"].keys()) == {"state", "heartbeat_age_seconds"}
    assert payload["worker"]["state"] == "absent"
    assert payload["worker"]["heartbeat_age_seconds"] is None

    assert set(payload["llm"].keys()) == {"provider", "local", "web_summaries"}
    assert isinstance(payload["llm"]["web_summaries"], bool)


def test_api_status_never_leaks_api_keys(status_client, monkeypatch):
    client, _ = status_client
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret123")
    monkeypatch.setenv("ANNULI_SUMMARY_PROVIDER", "openrouter")

    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert b"secret123" not in resp.data

    payload = resp.get_json()
    assert payload["llm"]["provider"] == "openrouter"
    assert payload["llm"]["local"] is False
    assert payload["llm"]["web_summaries"] is False


def test_api_status_handles_source_failure_gracefully(status_client, monkeypatch):
    client, _ = status_client

    def _boom():
        raise RuntimeError("index summary exploded")

    monkeypatch.setattr(web, "load_index_summary", _boom)

    resp = client.get("/api/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["sessions"] is None
    assert any("sessions:" in err and "index summary exploded" in err for err in payload["errors"])
    assert payload["index"] is not None
    assert payload["search"] is not None
    assert payload["worker"] is not None
    assert payload["llm"] is not None


def test_api_status_worker_ok_and_stale_states(status_client, monkeypatch):
    client, output_dir = status_client
    worker_dir = output_dir / "worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "state.json").write_text("{}", encoding="utf-8")
    hb = worker_dir / "heartbeat"
    hb.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("ANNULI_WORKER_INTERVAL", "10")

    resp_ok = client.get("/api/status")
    assert resp_ok.status_code == 200
    worker_ok = resp_ok.get_json()["worker"]
    assert worker_ok["state"] == "ok"
    assert isinstance(worker_ok["heartbeat_age_seconds"], int)

    stale_epoch = time.time() - 60
    os.utime(hb, (stale_epoch, stale_epoch))
    resp_stale = client.get("/api/status")
    assert resp_stale.status_code == 200
    worker_stale = resp_stale.get_json()["worker"]
    assert worker_stale["state"] == "stale"
    assert worker_stale["heartbeat_age_seconds"] >= 59


def test_api_status_enforces_host_allowlist(status_client):
    client, _ = status_client
    assert client.get("/api/status", headers={"Host": "attacker.example:5000"}).status_code == 400
    assert client.get("/api/status", headers={"Host": "localhost"}).status_code == 200


def test_rendered_page_includes_status_bar(status_client):
    client, _ = status_client
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="statusBar"' in html
    assert 'role="status"' in html
