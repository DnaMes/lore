"""Verify ``--help`` succeeds and emits usage for every CLI command path.

Walks argparse subparsers recursively for ``lore`` and ``lore-session`` and
also covers ``lore-web``, which has no subcommands.
"""

from __future__ import annotations

import argparse

import pytest


def _subparser_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    """Return the first _SubParsersAction on *parser*, if any."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _collect_command_paths(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Recursively collect all command paths registered under *parser*."""
    paths: list[tuple[str, ...]] = []
    action = _subparser_action(parser)
    if action is None:
        return paths
    for name, subparser in action.choices.items():
        path = prefix + (name,)
        paths.append(path)
        paths.extend(_collect_command_paths(subparser, path))
    return paths


def _assert_help_prints_usage(parser: argparse.ArgumentParser, path: tuple[str, ...], capsys):
    """Run ``path + --help`` through *parser* and assert a usage line is printed."""
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(list(path) + ["--help"])
    assert excinfo.value.code == 0, f"{path} --help exited with {excinfo.value.code}"
    out = capsys.readouterr().out
    assert "usage" in out.lower(), f"{path} --help did not print a usage line"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize test functions by the command paths they request."""
    if "lore_path" in metafunc.fixturenames:
        from lore_cli import build_parser as _lore_build_parser

        paths = _collect_command_paths(_lore_build_parser())
        metafunc.parametrize("lore_path", paths, ids=[" ".join(p) for p in paths])
    elif "lore_session_path" in metafunc.fixturenames:
        from lore_session_cli import build_parser as _session_build_parser

        paths = _collect_command_paths(_session_build_parser())
        metafunc.parametrize("lore_session_path", paths, ids=[" ".join(p) for p in paths])


def test_lore_command_help(lore_path: tuple[str, ...], capsys):
    """Every ``lore <command-path> --help`` exits 0 and prints usage."""
    from lore_cli import build_parser

    _assert_help_prints_usage(build_parser(), lore_path, capsys)


def test_lore_session_command_help(lore_session_path: tuple[str, ...], capsys):
    """Every ``lore-session <command-path> --help`` exits 0 and prints usage."""
    from lore_session_cli import build_parser

    _assert_help_prints_usage(build_parser(), lore_session_path, capsys)


def test_lore_root_help(capsys):
    """``lore --help`` exits 0 and prints usage."""
    from lore_cli import build_parser

    _assert_help_prints_usage(build_parser(), (), capsys)


def test_lore_session_root_help(capsys):
    """``lore-session --help`` exits 0 and prints usage."""
    from lore_session_cli import build_parser

    _assert_help_prints_usage(build_parser(), (), capsys)


def test_lore_web_help(capsys):
    """``lore-web --help`` exits 0 and prints usage."""
    from lore.cli.web import build_parser

    _assert_help_prints_usage(build_parser(), (), capsys)
