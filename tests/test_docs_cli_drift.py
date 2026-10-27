"""Pin docs/CLI.md against the argparse parser in lore_cli.py.

Every command path registered by ``build_parser()`` (e.g. ``memory`` and
``memory review``) must appear in docs/CLI.md, either as a heading (``## memory``
with ``### review`` under it) or as a literal ``lore memory review`` mention
(such as the ``--help`` usage block). Conversely, any heading that looks like a
command path of the form ``lore <cmd>`` must exist in the parser, so stale
documentation cannot survive a command rename.

README is deliberately exempt: it is an overview, not a complete reference.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from lore_cli import build_parser

CLI_MD = Path(__file__).resolve().parents[1] / "docs" / "CLI.md"

_HEADING_RE = re.compile(r"^(#+) (.+)$", re.MULTILINE)
_LORE_COMMAND_HEADING_RE = re.compile(r"^lore [a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*$")


def _subparser_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    """Return the first _SubParsersAction on *parser*, if any."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> list[str]:
    """Recursively collect registered command paths (``list``, ``memory review``...)."""
    action = _subparser_action(parser)
    if action is None:
        return []
    paths: list[str] = []
    for name, subparser in action.choices.items():
        path = prefix + (name,)
        paths.append(" ".join(path))
        paths.extend(_command_paths(subparser, path))
    return paths


def _cli_md() -> str:
    return CLI_MD.read_text(encoding="utf-8")


def _heading_paths(text: str) -> set[str]:
    """Heading texts, with level-3+ headings prefixed by their parent headings.

    ``## memory`` followed by ``### review`` therefore yields both ``memory``
    and ``memory review``, matching the command path naming of the parser.
    """
    paths: set[str] = set()
    parents: dict[int, tuple[str, ...]] = {}
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip()
        parent = parents.get(level - 1, ())
        parents[level] = parent + (title,)
        paths.add(" ".join(parent + (title,)))
    return paths


@pytest.mark.parametrize("command", _command_paths(build_parser()))
def test_every_parser_command_is_documented(command: str) -> None:
    """Each parser command path is a heading or a ``lore <cmd>`` mention in CLI.md."""
    text = _cli_md()
    assert command in _heading_paths(text) or f"lore {command}" in text, (
        f"docs/CLI.md does not document 'lore {command}': "
        "add a heading or a 'lore " + command + "' mention"
    )


def test_command_looking_headings_exist_in_parser() -> None:
    """Headings of the form ``lore <cmd>`` in CLI.md must be real parser commands."""
    parser_paths = set(_command_paths(build_parser()))
    for heading in _heading_paths(_cli_md()):
        if _LORE_COMMAND_HEADING_RE.match(heading):
            assert heading in parser_paths, (
                f"docs/CLI.md heading 'lore {heading.split(' ', 1)[1]}' "
                "does not exist in the lore parser"
            )
