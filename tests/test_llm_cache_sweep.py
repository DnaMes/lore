"""A hard kill must not leave provider-cache temp files behind forever.

Every provider writes its cache entry to a hidden ``.<key>.<random>.tmp`` file and
renames it into place. An error the process survives unlinks the temp file, but a
``SIGKILL`` between the write and the rename strands it, and nothing ever removes a
cache file the providers did not write. The providers now sweep those leftovers
when they open their cache directory.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lore.llm.base import LLMConfig
from lore.llm.cache_io import sweep_temp_files
from lore.llm.gemini import GeminiProvider
from lore.llm.jev import JevProvider
from lore.llm.ollama import OllamaProvider
from lore.llm.openrouter import OpenRouterProvider

STALE_AGE_SECONDS = 7200.0


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    root = tmp_path / "llm_cache"
    root.mkdir()
    return root


def _stranded(cache_dir: Path, name: str = ".abc123.deadbeef.tmp") -> Path:
    """A temp file an interrupted write left behind, two hours old."""
    path = cache_dir / name
    path.write_text('{"content": "half', encoding="utf-8")
    old = time.time() - STALE_AGE_SECONDS
    os.utime(path, (old, old))
    return path


def test_sweep_removes_a_stranded_temp_file(cache_dir: Path):
    stranded = _stranded(cache_dir)

    assert sweep_temp_files(cache_dir) == 1
    assert not stranded.exists()


def test_sweep_keeps_a_write_that_is_still_in_flight(cache_dir: Path):
    in_flight = cache_dir / ".abc123.cafe.tmp"
    in_flight.write_text("{}", encoding="utf-8")

    assert sweep_temp_files(cache_dir) == 0
    assert in_flight.exists()


def test_sweep_never_touches_cache_entries_or_other_files(cache_dir: Path):
    entry = cache_dir / "abc123.json"
    entry.write_text('{"content": "kept"}', encoding="utf-8")
    os.utime(entry, (0, 0))
    other = cache_dir / "notes.txt"
    other.write_text("kept", encoding="utf-8")
    os.utime(other, (0, 0))
    subdir = cache_dir / "nested"
    subdir.mkdir()

    assert sweep_temp_files(cache_dir) == 0
    assert entry.exists() and other.exists() and subdir.is_dir()


def test_sweep_keeps_a_temp_file_the_providers_did_not_write(cache_dir: Path):
    """``.notes.tmp`` is the user's, not an interrupted cache write."""
    foreign = cache_dir / ".notes.tmp"
    foreign.write_text("kept", encoding="utf-8")
    os.utime(foreign, (0, 0))

    assert sweep_temp_files(cache_dir) == 0
    assert foreign.exists()


def test_sweep_never_tries_to_unlink_a_directory(cache_dir: Path):
    """A directory that happens to carry the temp-file shape is not a temp file."""
    directory = cache_dir / ".abc.def.tmp"
    directory.mkdir()
    os.utime(directory, (0, 0))

    assert sweep_temp_files(cache_dir) == 0
    assert directory.is_dir()


def test_sweep_is_quiet_when_the_directory_is_missing(tmp_path: Path):
    assert sweep_temp_files(tmp_path / "does-not-exist") == 0


def test_sweep_honours_its_age_bound(cache_dir: Path):
    old = _stranded(cache_dir, ".old.1.tmp")
    young = cache_dir / ".young.1.tmp"
    young.write_text("{}", encoding="utf-8")

    assert sweep_temp_files(cache_dir, max_age_seconds=STALE_AGE_SECONDS - 60) == 1
    assert not old.exists()
    assert young.exists()


@pytest.mark.parametrize(
    "factory",
    [
        lambda cache_dir: GeminiProvider(LLMConfig(provider="gemini", cache_dir=str(cache_dir))),
        lambda cache_dir: OllamaProvider(LLMConfig(provider="ollama", cache_dir=str(cache_dir))),
        lambda cache_dir: JevProvider(LLMConfig(provider="jev", cache_dir=str(cache_dir))),
        lambda cache_dir: OpenRouterProvider(
            LLMConfig(provider="openrouter", cache_dir=str(cache_dir))
        ),
    ],
    ids=["gemini", "ollama", "jev", "openrouter"],
)
def test_a_provider_sweeps_its_cache_directory_when_it_opens_it(cache_dir, factory):
    stranded = _stranded(cache_dir)

    provider = factory(cache_dir)
    assert provider._ensure_cache_dir() == cache_dir

    assert not stranded.exists()


def test_a_provider_with_the_cache_disabled_does_not_touch_it(cache_dir: Path):
    stranded = _stranded(cache_dir)
    provider = GeminiProvider(
        LLMConfig(provider="gemini", cache_dir=str(cache_dir), cache_enabled=False)
    )

    assert provider._ensure_cache_dir() is None
    assert stranded.exists()
