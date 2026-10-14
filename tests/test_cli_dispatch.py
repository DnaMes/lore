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
import sys

import pytest

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


def test_main_propagates_handler_exit_code(monkeypatch):
    """A non-zero ``int`` returned by a handler becomes the process exit code (#198).

    ``main()`` used to discard the handler result, so every command exited 0.
    """
    monkeypatch.setattr(sys, "argv", ["lore", "list"])
    monkeypatch.setitem(COMMANDS, "list", lambda args: 1)
    with pytest.raises(SystemExit) as excinfo:
        lore_cli.main()
    assert excinfo.value.code == 1


def test_main_treats_zero_and_none_as_success(monkeypatch):
    """``0`` and ``None`` (the majority of handlers) keep a clean exit (#198)."""
    for value in (0, None):
        monkeypatch.setattr(sys, "argv", ["lore", "list"])
        monkeypatch.setitem(COMMANDS, "list", lambda args, _v=value: _v)
        assert lore_cli.main() is None  # no SystemExit raised


def test_unknown_or_missing_command_prints_help(capsys):
    # No command at all → handler is None → help is printed, no crash.
    parser = build_parser()
    args = parser.parse_args([])
    assert COMMANDS.get(args.command) is None
    parser.print_help()
    out = capsys.readouterr().out
    assert "usage: lore" in out
