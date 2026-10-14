"""B7.1: Claude Code retention warning in ``lore check`` (read-only)."""

from __future__ import annotations

import json
import os
import time
from argparse import Namespace
from pathlib import Path

import lore_cli
from lore.utils.claude_retention import (
    SNIPPET_CLEANUP_365,
    claude_projects_dir,
    read_cleanup_period_days,
    retention_check_lines,
)

SECONDS_PER_DAY = 86400


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def snapshot_claude_config(home: Path) -> dict[str, tuple[float, int]]:
    """Map relative paths under ``home/.claude`` to ``(mtime, size)`` for tests."""
    root = home / ".claude"
    out: dict[str, tuple[float, int]] = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(root))
            st = path.stat()
            out[rel] = (st.st_mtime, st.st_size)
    return out


def _claude_tree(home: Path) -> Path:
    return home / ".claude"


def _projects_with_transcript(home: Path, name: str = "sess.jsonl", age_days: float = 26.0) -> Path:
    proj = _claude_tree(home) / "projects" / "-test"
    proj.mkdir(parents=True)
    path = proj / name
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    mtime = time.time() - age_days * SECONDS_PER_DAY
    os.utime(path, (mtime, mtime))
    return path


def _settings(home: Path, content: str | bytes, local: bool = False) -> Path:
    base = _claude_tree(home)
    base.mkdir(parents=True, exist_ok=True)
    fname = "settings.local.json" if local else "settings.json"
    path = base / fname
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _run_check(home: Path, monkeypatch) -> str:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        lore_cli.cmd_check(Namespace(deep=False, output_dir=None))
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_no_claude_dir_no_retention_lines(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    out = _run_check(home, monkeypatch)
    assert "Retention" not in out


def test_projects_without_settings_warns_default(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _projects_with_transcript(home, age_days=26.0)
    before = snapshot_claude_config(home)
    out = _run_check(home, monkeypatch)
    after = snapshot_claude_config(home)
    assert before == after
    assert "Retention:" in out
    assert "after 30 days (default 30)" in out
    assert SNIPPET_CLEANUP_365 in out
    assert "1 transcripts are within 5 days of deletion" in out


def test_cleanup_period_7_warns(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _settings(home, json.dumps({"cleanupPeriodDays": 7}))
    _projects_with_transcript(home, age_days=3.0)
    out = _run_check(home, monkeypatch)
    assert "after 7 days" in out
    assert "within 5 days of deletion" in out


def test_cleanup_period_365_ok_line(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _settings(home, json.dumps({"cleanupPeriodDays": 365}))
    _projects_with_transcript(home)
    out = _run_check(home, monkeypatch)
    assert "Retention: cleanupPeriodDays=365" in out
    assert "within 5 days" not in out


def test_invalid_cleanup_period_string(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _settings(home, json.dumps({"cleanupPeriodDays": "x"}))
    _projects_with_transcript(home)
    out = _run_check(home, monkeypatch)
    assert "invalid value" in out.lower()


def test_invalid_json_treated_as_unset(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _settings(home, "{not json")
    _projects_with_transcript(home)
    out = _run_check(home, monkeypatch)
    assert "default 30" in out


def test_huge_settings_skipped(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    _settings(home, b" " * (256 * 1024 + 1))
    _projects_with_transcript(home)
    out = _run_check(home, monkeypatch)
    assert "default 30" in out


def test_claude_config_dir_overrides_home(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    alt = tmp_path / "alt-claude"
    alt.mkdir()
    (alt / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 365}), encoding="utf-8")
    _projects_with_transcript(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    days, kind = read_cleanup_period_days()
    assert days == 365 and kind == "ok"


def test_settings_local_used_when_settings_missing_key(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    base = _claude_tree(home)
    base.mkdir(parents=True)
    (base / "settings.json").write_text("{}", encoding="utf-8")
    _settings(home, json.dumps({"cleanupPeriodDays": 90}), local=True)
    monkeypatch.setenv("HOME", str(home))
    days, kind = read_cleanup_period_days()
    assert days == 90 and kind == "ok"


def test_retention_lines_unit_near_deletion_count(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    proj = _claude_tree(home) / "projects" / "-test"
    proj.mkdir(parents=True)
    for name, age in (("a.jsonl", 26.0), ("b.jsonl", 10.0)):
        path = proj / name
        path.write_text("x\n", encoding="utf-8")
        mtime = time.time() - age * SECONDS_PER_DAY
        os.utime(path, (mtime, mtime))
    monkeypatch.setenv("HOME", str(home))
    lines = retention_check_lines()
    assert any("1 transcripts are within 5 days of deletion" in ln for ln in lines)


def test_claude_projects_dir_respects_home(monkeypatch, tmp_path):
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    assert claude_projects_dir() == home / ".claude" / "projects"


def test_transcripts_follow_claude_config_dir(monkeypatch, tmp_path):
    home = _fake_home(tmp_path)
    alt = tmp_path / "alt"
    proj = alt / "projects" / "p"
    proj.mkdir(parents=True)
    path = proj / "s.jsonl"
    path.write_text("x\n", encoding="utf-8")
    old = time.time() - 27 * SECONDS_PER_DAY
    os.utime(path, (old, old))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    assert claude_projects_dir() == alt / "projects"
    assert any("1 transcripts are within 5 days" in ln for ln in retention_check_lines())


def test_retention_lines_swallow_filesystem_errors(monkeypatch):
    def boom() -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr("lore.utils.claude_retention.has_claude_transcripts", boom)
    assert retention_check_lines() == []
