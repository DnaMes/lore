"""Per-source archive health (#162).

A sync that reports only "400 sessions" hides what happened to the other files:
filtered as low quality, unsupported format, failed, unavailable, or explicitly
deleted. Every source gets a row and every state a counter.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage.schema import initialise
from lore.storage.source_health import (
    HEALTH_STATES,
    list_source_health,
    record_failure,
    record_filtered,
    record_source_state,
    source_health_summary,
    state_for_capture,
)
from lore.storage.writer import write_sessions


def _session(source_path: str, sid: str = "s-health") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Health",
        source_path=source_path,
        messages=[UnifiedMessage(role=Role.USER, content="hello", timestamp=datetime(2026, 1, 1))],
    )


def _write(tmp_path, session):
    db = tmp_path / "state" / "index_v2.sqlite"
    write_sessions(db, [session])
    return initialise(db)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_states_are_a_closed_set():
    assert HEALTH_STATES == (
        "captured",
        "parsed",
        "filtered",
        "unsupported",
        "failed",
        "unavailable",
        "deleted",
    )
    with pytest.raises(ValueError):
        from lore.storage.source_health import record_source_state

        record_source_state(None, source_identity="x", tool="t", source_path="/x", state="nonsense")


def test_capture_state_mapping_matches_the_issue_vocabulary():
    assert state_for_capture("created", "parsed") == "parsed"
    assert state_for_capture("reused", "stale") == "captured"
    assert state_for_capture("created", "unknown") == "unsupported"
    assert state_for_capture("created", "malformed") == "unsupported"
    assert state_for_capture("excluded") == "unavailable"
    assert state_for_capture("unavailable") == "unavailable"


def test_writer_records_a_parsed_source(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text('{"type": "summary"}\n', encoding="utf-8")

    conn = _write(tmp_path, _session(str(source)))

    summary = source_health_summary(conn)
    assert summary["sources_tracked"] == 1
    assert summary["states"]["parsed"]["sources"] == 1
    assert summary["states"]["parsed"]["sessions_captured"] == 1
    records = list_source_health(conn)
    assert records[0]["state"] == "parsed"
    assert records[0]["source_path"] == str(source)


def test_repeated_syncs_accumulate_counters(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text('{"type": "summary"}\n', encoding="utf-8")
    db = tmp_path / "state" / "index_v2.sqlite"

    write_sessions(db, [_session(str(source))])
    write_sessions(db, [_session(str(source))])

    summary = source_health_summary(initialise(db))
    assert summary["sources_tracked"] == 1
    assert summary["states"]["parsed"]["sessions_captured"] == 2


def test_credential_source_is_recorded_as_unavailable_not_stored(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")

    conn = _write(tmp_path, _session(str(secret)))

    summary = source_health_summary(conn)
    assert summary["states"]["unavailable"]["sources"] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


def test_missing_source_is_recorded_as_unavailable(tmp_path):
    conn = _write(tmp_path, _session(str(tmp_path / "gone.jsonl")))
    assert source_health_summary(conn)["states"]["unavailable"]["sources"] == 1


def test_filtered_failed_and_deleted_rows_are_reported_per_source(tmp_path):
    conn = initialise(tmp_path / "health.sqlite")
    record_filtered(conn, tool="claude-code", source_path="/x/a.jsonl", reason="too_few_prompts")
    record_failure(conn, tool="codex", source_path="/x/b.jsonl", error="decode error")
    record_source_state(
        conn,
        source_identity="claude-code:local:/x/c.jsonl",
        tool="claude-code",
        source_path="/x/c.jsonl",
        state="deleted",
    )
    conn.commit()

    summary = source_health_summary(conn)
    assert summary["states"]["filtered"]["sessions_filtered"] == 1
    assert summary["states"]["failed"]["sessions_failed"] == 1
    assert summary["states"]["deleted"]["sessions_filtered"] == 1
    assert summary["states"]["filtered"]["sources"] == 1

    filtered = list_source_health(conn, state="filtered")
    assert filtered[0]["detail"] == {"reason": "too_few_prompts"}
    assert list_source_health(conn, state="parsed") == []


def test_unknown_state_filter_is_rejected(tmp_path):
    conn = initialise(tmp_path / "health.sqlite")
    with pytest.raises(ValueError):
        list_source_health(conn, state="not-a-state")


def test_limit_is_applied(tmp_path):
    conn = initialise(tmp_path / "health.sqlite")
    for index in range(5):
        record_source_state(
            conn,
            source_identity=f"tool:local:/x/{index}.jsonl",
            tool="tool",
            source_path=f"/x/{index}.jsonl",
            state="parsed",
        )
    conn.commit()
    assert len(list_source_health(conn, limit=2)) == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["lore", *argv])
    code = lore_cli.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_sources_json_reports_summary_and_records(tmp_path, monkeypatch, capsys):
    source = tmp_path / "session.jsonl"
    source.write_text('{"type": "summary"}\n', encoding="utf-8")
    _write(tmp_path, _session(str(source)))

    code, out, _ = _run_cli(
        monkeypatch,
        capsys,
        "--output-dir",
        str(tmp_path / "state"),
        "sources",
        "--json",
    )

    assert code in (0, None)
    payload = json.loads(out)
    assert payload["summary"]["states"]["parsed"]["sources"] == 1
    assert payload["sources"][0]["source_path"] == str(source)


def test_cli_sources_text_lists_states(tmp_path, monkeypatch, capsys):
    source = tmp_path / "session.jsonl"
    source.write_text("{}", encoding="utf-8")
    _write(tmp_path, _session(str(source)))

    code, out, _ = _run_cli(monkeypatch, capsys, "--output-dir", str(tmp_path / "state"), "sources")

    assert code in (0, None)
    assert "Archive source health" in out
    assert "parsed" in out
    assert "Sources tracked: 1" in out


def test_cli_sources_without_a_store_exits_nonzero(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, capsys, "--output-dir", str(tmp_path / "empty"), "sources")
    assert excinfo.value.code == 1
    assert "No archive store found" in capsys.readouterr().out


def test_cli_check_reports_archive_health(tmp_path, monkeypatch, capsys):
    source = tmp_path / "session.jsonl"
    source.write_text("{}", encoding="utf-8")
    _write(tmp_path, _session(str(source)))

    code, out, _ = _run_cli(monkeypatch, capsys, "--output-dir", str(tmp_path / "state"), "check")

    assert code in (0, None)
    assert "Archive health: 1 source file(s) tracked" in out
