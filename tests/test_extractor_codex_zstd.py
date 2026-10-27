"""Codex ``.jsonl.zst`` rollouts: parsed with ``zstandard``, skipped with a warning without it."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest

from lore.extractors.codex import CodexExtractor


def _meta(session_id: str) -> dict:
    payload = {"id": session_id, "cli_version": "9.9.9", "cwd": "/work/zstd-proj"}
    return {"type": "session_meta", "timestamp": "2025-06-15T10:00:00", "payload": payload}


def _user(text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2025-06-15T10:01:00",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _write_zst(path: Path, session_id: str) -> None:
    zstd = pytest.importorskip("zstandard")
    records = [_meta(session_id), _user("first zstd prompt"), _user("second zstd prompt")]
    plain = "\n".join(json.dumps(r) for r in records) + "\n"
    path.write_bytes(zstd.ZstdCompressor().compress(plain.encode("utf-8")))


def _sessions_dir(home: Path) -> Path:
    d = home / ".codex" / "sessions" / "2025" / "06" / "15"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_jsonl_zst_fixture_is_parsed(home):
    pytest.importorskip("zstandard")
    _write_zst(_sessions_dir(home) / "rollout-fixture.jsonl.zst", "zst-fixture-sess")

    extractor = CodexExtractor()
    sessions = list(extractor.extract_sessions())

    assert [s.session_id for s in sessions] == ["zst-fixture-sess"]
    assert sessions[0].user_prompt_count >= 2
    assert extractor.discovery.get("skipped_compressed", 0) == 0


def test_missing_zstandard_skips_with_one_warning_per_run(home, monkeypatch, caplog):
    # The fixture bytes need not be valid zstd: the import fails before any decoding.
    sessions_dir = _sessions_dir(home)
    (sessions_dir / "rollout-a.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd" + b"x" * 32)
    (sessions_dir / "rollout-b.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd" + b"y" * 32)

    real_import = importlib.import_module

    def _block_zstd(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _block_zstd)

    extractor = CodexExtractor()
    with caplog.at_level(logging.WARNING, logger="lore.extractors.codex"):
        sessions = list(extractor.extract_sessions())

    assert sessions == []
    assert extractor.discovery.get("skipped_compressed", 0) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "zstandard" in warnings[0].getMessage()
    assert "lore[zstd]" in warnings[0].getMessage()

    # A second run resets the counter, so the warning is emitted once again.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lore.extractors.codex"):
        list(extractor.extract_sessions())
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
