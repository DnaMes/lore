"""Tests for annuli_home(): ~/.annuli is the data dir; the legacy ~/.ai-history is ignored."""

from __future__ import annotations

import annuli.utils.paths as paths


def test_annuli_home_returns_dot_annuli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    home = paths.annuli_home()
    assert home == tmp_path / ".annuli"


def test_legacy_ai_history_dir_is_not_migrated(tmp_path, monkeypatch):
    """The ~/.ai-history auto-migration is gone: the legacy dir is left untouched."""
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    legacy = tmp_path / ".ai-history"
    legacy.mkdir()
    (legacy / "index.json").write_text("{}", encoding="utf-8")

    home = paths.annuli_home()

    assert home == tmp_path / ".annuli"
    assert not home.exists()
    assert (legacy / "index.json").exists()
