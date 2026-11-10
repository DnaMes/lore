"""Session title quality heuristics (skill boilerplate, injected noise)."""

from __future__ import annotations

import pytest

from lore.exporters.index import is_low_quality_title

_BOILERPLATE_TITLES = [
    "Base directory for this skill: /home/dnames/.agents/skills/using-superpowers",
    "References are relative to /home/dnames/.agents/skills/using-superpowers. # Shared workflow:",
    '<skill name=using-superpowers location="/home/dnames/.agents/skills/using-superpowers">',
    "using-superpowers /using-sup...",
    "# Shared workflow: load skills first",
]

_GOOD_TITLES = [
    "Refactor auth middleware",
    "Kickzo ships",
    "Please set the base directory for assets in the config file",
]


@pytest.mark.parametrize("title", _BOILERPLATE_TITLES)
def test_skill_and_injected_boilerplate_is_low_quality(title: str) -> None:
    assert is_low_quality_title(title) is True


@pytest.mark.parametrize("title", _GOOD_TITLES)
def test_normal_titles_are_not_low_quality(title: str) -> None:
    assert is_low_quality_title(title) is False
