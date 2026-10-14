"""Summary card on session detail and list one-liner (B5.4, #197)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import lore.llm
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_jobs
from lore.interfaces.session_view_prep import at_a_glance
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.services.summarize import SUMMARY_PROMPT_VERSION
from lore.storage import initialise
from lore.storage.summaries import save_summary, summary_fingerprint
from lore.storage.writer import v2_db_path, write_sessions

T0 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
SID = "b54-card-0001-0001-0001-0001-000000000001"
SID_B = "b54-card-0002-0002-0002-0002-000000000002"
MODEL_LABEL = "ollama/llama3.2"
SENTINEL = "SENTINEL_TRANSCRIPT_BODY_DO_NOT_PRINT"


def _index_entry(*extra_sessions: dict) -> dict:
    base = {
        "id": SID,
        "tool": "claude-code",
        "title": "Summary card session",
        "display_title": "Summary card session",
        "created": "2026-04-01T10:00:00",
        "updated": "2026-04-01T10:05:00",
        "project": "/repo/lore",
        "messages": 3,
        "prompts": 1,
    }
    rows = [dict(base)]
    for extra in extra_sessions:
        rows.append({**base, **extra})
    return {
        "sessions": rows,
        "stats": {
            "total_sessions": len(rows),
            "total_messages": 3,
            "by_tool": {},
            "by_project": {},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }


def _session(*, with_tools: bool = False, user_text: str = "Fix the bug please") -> UnifiedSession:
    tool_calls = []
    if with_tools:
        tool_calls = [
            {"tool": "Read", "input": {"path": "/src/a.py"}, "output": "ok", "status": "ok"},
            {"tool": "bash", "input": {"command": "false"}, "output": "fail", "status": "failed"},
        ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=SID,
        created_at=T0,
        last_updated=datetime(2026, 4, 1, 10, 5, 0, tzinfo=timezone.utc),
        project_path="/repo/lore",
        title="Summary card session",
        messages=[
            UnifiedMessage(Role.USER, user_text, T0),
            UnifiedMessage(
                Role.ASSISTANT,
                "Done",
                datetime(2026, 4, 1, 10, 1, 0, tzinfo=timezone.utc),
                tool_calls=tool_calls,
            ),
        ],
    )


def _enriched(session: UnifiedSession | None = None):
    from lore.interfaces.web_formatting import format_message_content, format_tool_calls
    from lore.interfaces.web_services import enrich_session_for_detail
    from lore.utils.text_processing import format_thinking

    session = session or _session()
    enrich_session_for_detail(
        session,
        _index_entry()["sessions"][0],
        format_message_content,
        format_tool_calls,
        format_thinking,
    )
    return session


def _stored_payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "one_liner": "Shipped the fix",
        "goal": {"text": "fix bug", "cites": [0]},
        "outcome": {"status": "done", "text": "fixed <script>x</script>", "cites": [99]},
        "decisions": [{"text": "use cache", "cites": [0]}],
        "open_questions": [{"text": "roll out?", "cites": [0]}],
        "next_steps": [{"text": "deploy", "cites": [0]}],
        "topics": ["perf", "web"],
        "confidence": 0.9,
        "generated_with": MODEL_LABEL,
        "artifacts": {"files_read": [], "files_modified": [], "commands": [], "errors": []},
        "truncated": False,
        "chunks": 2,
    }
    body.update(overrides)
    return body


def _save_row(tmp_path: Path, payload: dict[str, Any], session_id: str = SID) -> None:
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{session_id}",
        session_id=session_id,
        fingerprint=summary_fingerprint("body", MODEL_LABEL, SUMMARY_PROMPT_VERSION, 1),
        model=MODEL_LABEL,
        prompt_version=SUMMARY_PROMPT_VERSION,
        compressor_version=1,
        payload=payload,
    )


class FakeProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(LLMConfig.for_transcripts(provider="fake", model="test"))

    def generate(self, prompt: str, system_prompt=None, max_tokens=None, temperature=None):
        return LLMResponse(content="{}", model="test", tokens_used=1)

    def generate_batch(self, prompts, system_prompt=None):
        return [self.generate(p) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self):
        return {"model": "test"}


@pytest.fixture()
def detail_client(monkeypatch):
    session = _enriched()
    monkeypatch.setattr(web, "load_index", lambda: _index_entry())
    monkeypatch.setattr(
        web,
        "_enriched_session_for_detail",
        lambda _sid, force_live=False: (session, []),
    )
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: set())
    return web.app.test_client()


def test_at_a_glance_empty_session():
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="x",
        created_at=T0,
        last_updated=T0,
        messages=[],
    )
    session.prompt_count = 0
    glance = at_a_glance(session)
    assert glance["prompts"] == 0
    assert glance["tool_calls"] == 0
    assert glance["first_prompt"] == ""


def test_at_a_glance_clamps_long_prompt():
    long_text = "α" * 400
    session = _enriched(_session(user_text=long_text))
    glance = at_a_glance(session)
    assert len(glance["first_prompt"]) <= 240
    assert glance["first_prompt"].endswith("…")
    assert "α" in glance["first_prompt"]


def test_at_a_glance_tools_and_files():
    session = _enriched(_session(with_tools=True))
    glance = at_a_glance(session)
    assert glance["tool_calls"] == 2
    assert glance["failed_tool_calls"] == 1
    assert glance["tools_used"]
    assert "/src/a.py" in glance["files_touched"]


def test_card_renders_stored_summary_fields(detail_client, tmp_path, monkeypatch):
    from lore.storage.summaries import get_summary

    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    _save_row(tmp_path, _stored_payload())
    assert get_summary(tmp_path, "session", f"session:{SID}") is not None
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert "AI summary" in html
    assert "Shipped the fix" in html
    assert "done" in html
    assert "perf" in html
    assert MODEL_LABEL in html
    assert "2 chunks" in html
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_card_stale_badge(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    _save_row(tmp_path, _stored_payload())
    monkeypatch.setattr("lore.interfaces.web_summary.is_summary_stale", lambda *_a, **_k: True)
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert "STALE" in html
    assert "Session changed since this summary" in html


def test_cite_links_only_when_anchor_exists(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    _save_row(tmp_path, _stored_payload())
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert 'href="#msg-0"' in html
    assert "#99" in html
    assert 'href="#msg-99"' not in html


def test_absent_summary_renders_at_a_glance_without_provider(detail_client, monkeypatch):
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise AssertionError("provider touched")

    monkeypatch.setattr(
        "lore.services.summarize_cli.build_mcp_summarize_provider",
        _boom,
    )
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert "At a glance" in html
    assert "Fix the bug please" in html
    assert calls["n"] == 0


def test_generate_disabled_shows_cli_without_env(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.delenv("LORE_SUMMARY_PROVIDER", raising=False)
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert "aria-disabled" in html or "disabled" in html
    assert f"lore summarize --session {SID}" in html
    assert "LORE_SUMMARY_PROVIDER=ollama" in html


def test_generate_enabled_with_local_ollama(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)

    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert 'data-action="generate-session-summary"' in html
    assert 'summary-generate-btn" disabled' not in html
    assert 'summary-generate-btn" aria-disabled="true"' not in html


def test_generate_disabled_with_remote_ollama_host(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.50:11434")
    html = detail_client.get(f"/session/{SID}?live=1").get_data(as_text=True)
    assert "disabled" in html
    assert "LORE_SUMMARY_PROVIDER=ollama" not in html or "loopback" in html.lower()


def test_post_summary_rejects_provider_params(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "ollama")
    resp = detail_client.post(f"/session/{SID}/summary?provider=gemini")
    assert resp.status_code == 400


@pytest.fixture()
def use_provider(monkeypatch):
    def install(provider: FakeProvider):
        monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
        return provider

    return install


def test_post_summary_starts_job_and_writes_row(detail_client, tmp_path, monkeypatch, use_provider):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "ollama")
    use_provider(FakeProvider())

    def fake_run(provider, output_dir, session_id, **kwargs):
        _save_row(output_dir, _stored_payload(one_liner="From job"), session_id=session_id)
        return "written"

    monkeypatch.setattr("lore.services.summarize.summarize_session", fake_run)

    real_thread = threading.Thread

    def sync_thread(target=None, args=(), kwargs=None, daemon=None):
        if target:
            target()
        return real_thread(target=lambda: None)

    monkeypatch.setattr(web_jobs.threading, "Thread", sync_thread)

    resp = detail_client.post(f"/session/{SID}/summary")
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    state = web_jobs._get_reload_job(job_id)
    assert state and state["status"] == "done"

    again = detail_client.post(f"/session/{SID}/summary")
    assert again.status_code == 202


def test_get_api_summary_allow_list_and_no_sentinel(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    payload = _stored_payload()
    payload["artifacts"]["errors"] = [{"seq": 1, "text": SENTINEL}]
    _save_row(tmp_path, payload)
    raw = detail_client.get(f"/api/session/{SID}/summary").get_data(as_text=True)
    assert SENTINEL not in raw
    body = json.loads(raw)
    assert body["status"] == "available"
    assert body["summary"]["one_liner"] == "Shipped the fix"
    assert "errors" not in body["summary"].get("artifacts", {})


def test_get_api_summary_404_deleted(detail_client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: {SID})
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [_session()])
    _save_row(tmp_path, _stored_payload())
    resp = detail_client.get(f"/api/session/{SID}/summary")
    assert resp.status_code == 404


def test_list_shows_star_only_when_summary_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: set())
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    idx = _index_entry({"id": SID_B, "title": "Other"})
    monkeypatch.setattr(web, "load_index", lambda: idx)
    _save_row(tmp_path, _stored_payload(), session_id=SID)
    client = web.app.test_client()
    html = client.get("/sessions").get_data(as_text=True)
    assert "✦" in html
    assert "Shipped the fix" in html
    assert html.count("✦") == 1


def test_rows_partial_matches_list(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    initialise(v2_db_path(tmp_path))
    monkeypatch.setattr(web, "load_index", lambda: _index_entry({"id": SID_B}))
    _save_row(tmp_path, _stored_payload())
    frag = web.app.test_client().get("/sessions/rows?page=1").get_data(as_text=True)
    assert "✦" in frag
    assert "Shipped the fix" in frag


def test_list_summaries_called_once(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    calls = []

    def spy(output_dir, level=None, session_ids=None):
        calls.append(list(session_ids or []))
        from lore.storage.summaries import list_summaries as real

        return real(output_dir, level=level, session_ids=session_ids)

    monkeypatch.setattr("lore.interfaces.web_summary.list_summaries", spy)
    monkeypatch.setattr(
        web,
        "load_index",
        lambda: _index_entry({"id": SID_B}, {"id": "b54-card-0003-0003-0003-000000000003"}),
    )
    web.app.test_client().get("/sessions")
    assert len(calls) == 1
    assert len(calls[0]) >= 2


def test_concurrent_summary_job_requests_share_one_job(monkeypatch):
    import threading

    from lore.interfaces import web_jobs

    started: list[str] = []
    gate = threading.Event()

    class FakeThread:
        def __init__(self, target, daemon=True):
            self._target = target

        def start(self):
            started.append("worker")
            gate.set()  # the worker is NOT run: the job stays "running"

    import types

    monkeypatch.setattr(web_jobs, "threading", types.SimpleNamespace(Thread=FakeThread))
    web_jobs.RELOAD_JOBS.clear()
    results: list[str] = []
    barrier = threading.Barrier(8)

    def request():
        barrier.wait()
        results.append(web_jobs._start_session_summary_job("sess-race"))

    threads = [threading.Thread(target=request) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(set(results)) == 1
    assert started == ["worker"]
    web_jobs.RELOAD_JOBS.clear()


def test_job_error_message_does_not_leak_exception_text(monkeypatch):
    from lore.interfaces import web_jobs

    web_jobs.RELOAD_JOBS.clear()
    monkeypatch.setattr(
        "lore.services.summarize_cli.web_summarize_enabled", lambda: True, raising=False
    )

    def boom():
        raise RuntimeError("/home/secret/path prompt: sk-" + "a" * 24)

    monkeypatch.setattr("lore.services.summarize_cli.build_mcp_summarize_provider", boom)
    job_id = web_jobs._start_session_summary_job("sess-err")
    for _ in range(200):
        state = web_jobs.RELOAD_JOBS.get(job_id, {})
        if state.get("status") != "running":
            break
        import time

        time.sleep(0.01)

    state = web_jobs.RELOAD_JOBS[job_id]
    assert state["status"] == "error"
    assert "secret" not in str(state) and "sk-" not in str(state)
    web_jobs.RELOAD_JOBS.clear()


def test_list_one_liner_marks_outdated_and_hides_deleted(tmp_path):
    from lore.interfaces.web_summary import session_summary_lines
    from lore.storage.summaries import save_summary

    for sid in ("fresh", "stale", "gone"):
        save_summary(
            tmp_path,
            level="session",
            scope_key=f"session:{sid}",
            session_id=sid,
            fingerprint="f",
            model="ollama/x",
            prompt_version=1,
            compressor_version=1,
            payload={"one_liner": f"line {sid}"},
        )
    (tmp_path / "deleted_sessions.json").write_text('{"session_ids": ["gone"]}', encoding="utf-8")

    lines = session_summary_lines(
        tmp_path,
        ["fresh", "stale", "gone"],
        {"fresh": "2000-01-01T00:00:00", "stale": "2999-01-01T00:00:00", "gone": "2000-01-01"},
    )

    assert set(lines) == {"fresh", "stale"}
    assert lines["fresh"]["outdated"] is False
    assert lines["stale"]["outdated"] is True
