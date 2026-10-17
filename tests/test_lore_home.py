"""Tests for lore_home(): ~/.lore is the data dir; the legacy ~/.ai-history is ignored."""

from __future__ import annotations

import lore.utils.paths as paths


def test_lore_home_returns_dot_lore(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    home = paths.lore_home()
    assert home == tmp_path / ".lore"


def test_legacy_ai_history_dir_is_not_migrated(tmp_path, monkeypatch):
    """The ~/.ai-history auto-migration is gone: the legacy dir is left untouched."""
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    legacy = tmp_path / ".ai-history"
    legacy.mkdir()
    (legacy / "index.json").write_text("{}", encoding="utf-8")

    home = paths.lore_home()

    assert home == tmp_path / ".lore"
    assert not home.exists()
    assert (legacy / "index.json").exists()
