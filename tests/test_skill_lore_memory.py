"""Pin skills/lore-memory/SKILL.md against the memory CLI it documents.

The skill is shipped to agents (and in the sdist), so a renamed subcommand or a
dropped flag must not be able to leave it lying. The test reads every fenced bash
command that starts with ``lore memory`` and checks the subcommand and each long
option against ``build_parser()``; it also checks the frontmatter a harness needs
to load the skill at all.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest
import yaml

from lore_cli import build_parser

SKILL_MD = Path(__file__).resolve().parents[1] / "skills" / "lore-memory" / "SKILL.md"

_COMMAND_RE = re.compile(r"^lore memory ([a-z][a-z0-9-]*)", re.MULTILINE)
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the parser has no subcommands")


def _memory_commands() -> dict[str, argparse.ArgumentParser]:
    return _subparsers(_subparsers(build_parser())["memory"])


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _skill_commands() -> list[tuple[str, set[str]]]:
    """Every ``lore memory <cmd>`` in a bash block, with the flags on its line(s)."""
    commands: list[tuple[str, set[str]]] = []
    for block in _FENCE_RE.findall(_skill_text()):
        logical = block.replace("\\\n", " ")
        for line in logical.splitlines():
            match = _COMMAND_RE.match(line.strip())
            if match:
                commands.append((match.group(1), set(_FLAG_RE.findall(line))))
    return commands


def _frontmatter() -> dict:
    """Parse the skill's frontmatter the way a harness does: real YAML, not a regex."""
    text = _skill_text()
    assert text.startswith("---\n"), "the skill must open with YAML frontmatter"
    block = text.split("---\n")[1]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), "the frontmatter must be a mapping"
    return parsed


def test_frontmatter_gives_a_harness_a_name_and_a_description():
    frontmatter = _frontmatter()
    assert frontmatter["name"] == "lore-memory"
    description = frontmatter["description"]
    assert isinstance(description, str) and description.strip()
    assert "Triggers:" in description, "the description names its triggers"


def test_every_documented_command_exists():
    documented = _skill_commands()
    assert documented, "no `lore memory` example found - did the fences change?"
    known = _memory_commands()
    unknown = sorted({name for name, _ in documented if name not in known})
    assert not unknown, f"the skill documents unknown memory subcommands: {unknown}"


def test_every_documented_flag_is_accepted():
    known = _memory_commands()
    problems: list[str] = []
    for name, flags in _skill_commands():
        if name not in known:  # reported by the command test
            continue
        accepted = {option for action in known[name]._actions for option in action.option_strings}
        for flag in sorted(flags - accepted):
            problems.append(f"lore memory {name} {flag}")
    assert not problems, f"the skill documents flags the CLI does not take: {problems}"


@pytest.mark.parametrize("subcommand", ["add", "search", "review", "context"])
def test_the_commands_the_skill_teaches_are_the_ones_agents_need(subcommand: str):
    """A guard against gutting the skill: the four load-bearing subcommands stay documented."""
    documented = {name for name, _ in _skill_commands()}
    assert subcommand in documented
