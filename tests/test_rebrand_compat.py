"""Rebrand Lore -> Annuli: legacy data dir, env vars and stored paths keep working."""

import os
from pathlib import Path

import pytest

import annuli
from annuli.utils import paths


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _store(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index_v2.sqlite").write_bytes(b"")
    return path


def test_home_defaults_to_annuli_when_nothing_exists(fake_home):
    assert paths.annuli_home() == fake_home / ".annuli"


def test_home_falls_back_to_legacy_store(fake_home):
    _store(fake_home / ".lore")
    assert paths.annuli_home() == fake_home / ".lore"


def test_bare_annuli_dir_does_not_hide_legacy_store(fake_home):
    (fake_home / ".annuli").mkdir()
    _store(fake_home / ".lore")
    assert paths.annuli_home() == fake_home / ".lore"


def test_annuli_store_wins_over_legacy(fake_home):
    _store(fake_home / ".annuli")
    _store(fake_home / ".lore")
    assert paths.annuli_home() == fake_home / ".annuli"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"ANNULI_X": "new"}, "new"),
        ({"LORE_X": "old"}, "old"),
        ({"ANNULI_X": "new", "LORE_X": "old"}, "new"),
        ({}, None),
    ],
)
def test_legacy_env_is_adopted(monkeypatch, capsys, env, expected):
    monkeypatch.delenv("ANNULI_X", raising=False)
    monkeypatch.delenv("LORE_X", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    annuli._adopt_legacy_env()
    assert os.environ.get("ANNULI_X") == expected
    err = capsys.readouterr().err
    assert ("LORE_X" in err) == (env == {"LORE_X": "old"})


def test_stored_legacy_path_reanchors_under_new_home(tmp_path, monkeypatch):
    from annuli.interfaces import web_data

    home = tmp_path / ".annuli"
    target = home / "projects" / "p" / "s.md"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", home)
    stored = "/home/someone/.lore/projects/p/s.md"
    assert web_data.resolve_export_path(stored) == target.resolve()
