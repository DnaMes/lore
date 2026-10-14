"""Tests for Codex origin, layout, and state_5 titles (task B2.2b)."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from lore.core.models import TitleSource
from lore.extractors.base import BaseExtractor
from lore.extractors.codex import CodexExtractor


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _sessions_dir(home: Path) -> Path:
    d = home / ".codex" / "sessions" / "2025" / "06" / "15"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archived_dir(home: Path) -> Path:
    d = home / ".codex" / "archived_sessions" / "bundle-a"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta(
    session_id: str = "sess-origin-01",
    cwd: str | None = "/work/proj",
    **payload_extra: object,
) -> dict:
    payload: dict = {"id": session_id, "cli_version": "9.9.9", **payload_extra}
    if cwd is not None:
        payload["cwd"] = cwd
    return {"type": "session_meta", "timestamp": "2025-06-15T10:00:00", "payload": payload}


def _one_user(text: str = "single user prompt") -> dict:
    return {
        "type": "response_item",
        "timestamp": "2025-06-15T10:01:00",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _rollout(home: Path, records: list[dict], name: str = "rollout-origin.jsonl") -> Path:
    path = _sessions_dir(home) / name
    _write_jsonl(path, records)
    return path


def _extract(home: Path) -> tuple[CodexExtractor, list]:
    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_parser_version_is_three():
    assert CodexExtractor.PARSER_VERSION == 3


def test_origin_exec_from_source_exec(home):
    _rollout(home, [_meta(source="exec"), _one_user()])
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert sessions[0].origin == "exec"


def test_origin_exec_from_originator_codex_exec(home):
    _rollout(home, [_meta(originator="codex_exec"), _one_user()])
    _, sessions = _extract(home)
    assert sessions[0].origin == "exec"


@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_origin_interactive_from_cli_or_vscode(home, source):
    _rollout(home, [_meta(source=source), _one_user()])
    _, sessions = _extract(home)
    assert sessions[0].origin == "interactive"


def test_origin_subagent_with_parent(home):
    parent = "parent-thread-abc"
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    _rollout(home, [_meta(source=source), _one_user()])
    _, sessions = _extract(home)
    s = sessions[0]
    assert s.origin == "subagent"
    assert s.relation == "subagent"
    assert s.parent_session_id == parent


def test_origin_subagent_without_parent(home):
    _rollout(home, [_meta(source={"subagent": {"thread_spawn": {}}}), _one_user()])
    _, sessions = _extract(home)
    s = sessions[0]
    assert s.origin == "subagent"
    assert s.relation == "subagent"
    assert s.parent_session_id is None


def test_origin_subagent_parent_fallback_payload(home):
    parent = "payload-parent-id"
    _rollout(
        home,
        [_meta(source={"subagent": {}}, parent_thread_id=parent), _one_user()],
    )
    _, sessions = _extract(home)
    assert sessions[0].parent_session_id == parent


def test_relation_fork_from_forked_from_id(home):
    fork_parent = "fork-source-id"
    _rollout(home, [_meta(forked_from_id=fork_parent), _one_user()])
    _, sessions = _extract(home)
    s = sessions[0]
    assert s.relation == "fork"
    assert s.parent_session_id == fork_parent


def test_fork_does_not_override_subagent_parent(home):
    sub_parent = "sub-parent"
    fork_parent = "fork-parent"
    source = {"subagent": {"thread_spawn": {"parent_thread_id": sub_parent}}}
    _rollout(
        home,
        [_meta(source=source, forked_from_id=fork_parent), _one_user()],
    )
    _, sessions = _extract(home)
    s = sessions[0]
    assert s.relation == "subagent"
    assert s.parent_session_id == sub_parent


@pytest.mark.parametrize("bad_source", [None, 42, ["cli"], {}])
def test_odd_source_types_yield_unknown_without_raising(home, bad_source):
    _rollout(home, [_meta(source=bad_source), _one_user()])
    _, sessions = _extract(home)
    assert sessions[0].origin == "unknown"


def test_exec_one_prompt_imported_interactive_one_prompt_dropped(home, monkeypatch):
    monkeypatch.setattr(BaseExtractor, "MIN_USER_PROMPTS", 3, raising=True)
    monkeypatch.delenv("LORE_MIN_USER_PROMPTS", raising=False)
    _rollout(
        home,
        [_meta(session_id="exec-one", source="exec"), _one_user("exec only")],
        name="rollout-exec.jsonl",
    )
    _rollout(
        home,
        [_meta(session_id="cli-one", source="cli"), _one_user("cli only")],
        name="rollout-cli.jsonl",
    )
    _, sessions = _extract(home)
    by_id = {s.session_id: s for s in sessions}
    assert "exec-one" in by_id
    assert by_id["exec-one"].origin == "exec"
    assert "cli-one" not in by_id


def test_archived_sessions_rollout_is_read(home):
    path = _archived_dir(home) / "rollout-archived.jsonl"
    _write_jsonl(path, [_meta(session_id="archived-sess"), _one_user()])
    _, sessions = _extract(home)
    assert any(s.session_id == "archived-sess" for s in sessions)


def test_jsonl_zst_skipped_when_zstandard_missing(home, monkeypatch):
    path = _sessions_dir(home) / "rollout-compressed.jsonl.zst"
    path.write_bytes(b"\x28\xb5\x2f\xfd" + b"x" * 32)
    real_import = importlib.import_module

    def _block_zstd(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _block_zstd)
    extractor, sessions = _extract(home)
    assert sessions == []
    assert extractor.discovery.get("skipped_compressed", 0) >= 1


def test_jsonl_zst_parsed_when_zstandard_available(home):
    zstd = pytest.importorskip("zstandard")
    records = [_meta(session_id="zst-sess"), _one_user()]
    plain = "\n".join(json.dumps(r) for r in records) + "\n"
    path = _sessions_dir(home) / "rollout-zst.jsonl.zst"
    path.write_bytes(zstd.ZstdCompressor().compress(plain.encode("utf-8")))
    _, sessions = _extract(home)
    assert any(s.session_id == "zst-sess" for s in sessions)


def _build_state_db(db_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, first_user_message TEXT)"
    )
    conn.executemany(
        "INSERT INTO threads VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_title_from_state_sqlite_by_rollout_basename(home):
    rollout = _rollout(home, [_meta(session_id="tid-1"), _one_user()], name="rollout-titled.jsonl")
    db = home / ".codex" / "state_5.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    _build_state_db(
        db,
        [
            (
                "tid-1",
                str(rollout),
                "Native Thread Title",
                "secret first message must not appear in logs",
            ),
        ],
    )
    after_write = db.read_bytes()
    _, sessions = _extract(home)
    assert sessions[0].title == "Native Thread Title"
    assert sessions[0].title_source == TitleSource.NATIVE
    assert db.read_bytes() == after_write
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()


def test_corrupt_state_sqlite_falls_back_without_raising(home):
    _rollout(home, [_meta(), _one_user("fallback title words here")])
    db = home / ".codex" / "state_5.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"not-a-sqlite-file" * 20)
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert sessions[0].title is not None


def test_automated_bypasses_prompt_floor_flag():
    assert CodexExtractor.AUTOMATED_BYPASSES_PROMPT_FLOOR is True


# ---------------------------------------------------------------------------
# Review findings (B2.2b): sidecars, bounded queries, duplicates, strict subagent
# ---------------------------------------------------------------------------


def _state_db(home: Path, rows: list[tuple]) -> Path:
    path = home / ".codex" / "state_5.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, first_user_message TEXT)"
    )
    conn.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def _one_session(home: Path, session_id: str = "sess-rev-01") -> list[dict]:
    return [_meta(session_id), _one_user("a real prompt for the title test")]


def test_symlinked_wal_sidecar_disables_native_titles(home, tmp_path):
    db = _state_db(home, [("sess-rev-01", "x/rollout-rev.jsonl", "Native Title", None)])
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"not a real wal")
    Path(f"{db}-wal").symlink_to(outside)
    _rollout(home, _one_session(home), "rollout-rev.jsonl")

    _, sessions = _extract(home)

    assert len(sessions) == 1
    assert sessions[0].title != "Native Title"


def test_oversized_sidecar_disables_native_titles(home, monkeypatch):
    db = _state_db(home, [("sess-rev-01", "x/rollout-rev.jsonl", "Native Title", None)])
    Path(f"{db}-wal").write_bytes(b"x" * 4096)
    monkeypatch.setattr("lore.extractors.codex._MAX_SQLITE_BYTES", db.stat().st_size + 100)
    _rollout(home, _one_session(home), "rollout-rev.jsonl")

    _, sessions = _extract(home)

    assert len(sessions) == 1
    assert sessions[0].title != "Native Title"


def test_title_field_sizes_and_row_count_are_bounded(home, monkeypatch):
    monkeypatch.setattr("lore.extractors.codex._MAX_THREAD_ROWS", 5)
    rows = [(f"id-{i}", f"x/rollout-{i}.jsonl", "T" * 5000, None) for i in range(50)]
    rows.append(("sess-rev-01", "x/rollout-rev.jsonl", "Z" * 5000, None))
    _state_db(home, rows)
    _rollout(home, _one_session(home), "rollout-rev.jsonl")

    from lore.extractors.codex import _load_thread_titles

    titles = _load_thread_titles(home / ".codex")

    assert len(titles) <= 10  # at most 5 rows, each indexed by id and by file name
    assert all(len(v) <= 200 for v in titles.values())


def test_same_session_in_live_and_archived_is_yielded_once(home):
    records = _one_session(home, "sess-dup-01")
    _rollout(home, records, "rollout-dup.jsonl")
    _write_jsonl(_archived_dir(home) / "rollout-dup.jsonl", records)

    extractor, sessions = _extract(home)

    assert [s.session_id for s in sessions] == ["sess-dup-01"]
    assert extractor.discovery.get("duplicate_sessions") == 1


@pytest.mark.parametrize("bad", ["subagent", ["subagent"], 5, None])
def test_non_dict_subagent_value_is_not_a_subagent(home, bad):
    records = [_meta("sess-bad-01", source={"subagent": bad}), _one_user()]
    _rollout(home, records, "rollout-bad.jsonl")

    _, sessions = _extract(home)

    assert all(s.origin != "subagent" for s in sessions)


def test_subagent_without_parent_falls_back_to_fork_source(home):
    records = [
        _meta("sess-sub-01", source={"subagent": {"thread_spawn": {}}}, forked_from_id="origin-1"),
        _one_user(),
    ]
    _rollout(home, records, "rollout-sub.jsonl")

    _, sessions = _extract(home)

    assert sessions[0].origin == "subagent"
    assert sessions[0].relation == "subagent"
    assert sessions[0].parent_session_id == "origin-1"
