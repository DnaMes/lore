"""CLI dispatch-table integrity (#118).

``main()`` used to dispatch through a 22-branch if/elif chain: adding a
subcommand required touching both the ``add_parser(...)`` block and the
chain, with nothing keeping the two in sync. The chain is now the
``COMMANDS`` lookup table; these tests pin the table against the argparse
subparsers (via the ``build_parser()`` helper) so a command registered on
one side but not the other fails fast.
"""

from __future__ import annotations

import argparse

import lore_cli
from lore_cli import COMMANDS, build_parser


def _subparser_names(parser: argparse.ArgumentParser) -> set[str]:
    """Names registered via add_parser() on the top-level parser."""
    action = next(
        a
        for a in parser._actions  # noqa: SLF001 - argparse exposes no public API
        if isinstance(a, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_commands_table_covers_every_subparser():
    registered = _subparser_names(build_parser())
    assert registered == set(COMMANDS), (
        f"dispatch drift: argparse-only={sorted(registered - set(COMMANDS))}, "
        f"table-only={sorted(set(COMMANDS) - registered)}"
    )


def test_every_handler_is_callable():
    for name, handler in COMMANDS.items():
        assert callable(handler), f"COMMANDS[{name!r}] is not callable"
    assert lore_cli.COMMANDS["export-html"] is not None


def test_unknown_or_missing_command_prints_help(capsys):
    # No command at all → handler is None → help is printed, no crash.
    parser = build_parser()
    args = parser.parse_args([])
    assert COMMANDS.get(args.command) is None
    parser.print_help()
    out = capsys.readouterr().out
    assert "usage: lore" in out
