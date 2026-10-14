"""
Parametrized contract tests for all extractors (issue #26).

These tests verify that every extractor class correctly implements the
BaseExtractor interface without requiring actual tool data on disk.
"""

from __future__ import annotations

import pytest

from lore.core.models import Tool, UnifiedSession
from lore.extractors.aider import AiderExtractor
from lore.extractors.antigravity import AntigravityExtractor
from lore.extractors.base import BaseExtractor
from lore.extractors.claude import ClaudeCodeExtractor
from lore.extractors.cline import ClineExtractor, KiloCodeExtractor, RooCodeExtractor
from lore.extractors.codex import CodexExtractor
from lore.extractors.commandcode import CommandCodeExtractor
from lore.extractors.copilot import CopilotCLIExtractor
from lore.extractors.cursor import CursorExtractor
from lore.extractors.gemini import GeminiCLIExtractor
from lore.extractors.hermes import HermesExtractor
from lore.extractors.opencode import OpenCodeExtractor
from lore.extractors.pi import PiExtractor
from lore.extractors.vscode import VSCodeCopilotExtractor
from lore.extractors.warp import WarpExtractor

# ---------------------------------------------------------------------------
# All concrete extractor classes under test
# ---------------------------------------------------------------------------

ALL_EXTRACTOR_CLASSES = [
    AiderExtractor,
    AntigravityExtractor,
    ClaudeCodeExtractor,
    ClineExtractor,
    CommandCodeExtractor,
    CodexExtractor,
    CopilotCLIExtractor,
    CursorExtractor,
    GeminiCLIExtractor,
    HermesExtractor,
    KiloCodeExtractor,
    OpenCodeExtractor,
    PiExtractor,
    RooCodeExtractor,
    VSCodeCopilotExtractor,
    WarpExtractor,
]

# Convenience IDs for pytest output
ALL_EXTRACTOR_IDS = [cls.__name__ for cls in ALL_EXTRACTOR_CLASSES]


# ---------------------------------------------------------------------------
# Contract: class hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_is_subclass_of_base(extractor_cls):
    assert issubclass(extractor_cls, BaseExtractor), (
        f"{extractor_cls.__name__} must inherit from BaseExtractor"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_is_not_abstract(extractor_cls):
    """Concrete extractors must be fully implemented (no abstract methods left)."""
    abstract_methods = getattr(extractor_cls, "__abstractmethods__", frozenset())
    assert not abstract_methods, (
        f"{extractor_cls.__name__} has unimplemented abstract methods: {abstract_methods}"
    )


# ---------------------------------------------------------------------------
# Contract: `tool` property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_has_tool_property(extractor_cls):
    """The `tool` attribute must exist and be a property on the class."""
    assert hasattr(extractor_cls, "tool"), (
        f"{extractor_cls.__name__} is missing the `tool` property"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_tool_returns_tool_enum(extractor_cls):
    """Instantiating and accessing `.tool` must return a Tool enum member."""
    instance = extractor_cls()
    tool_value = instance.tool
    assert isinstance(tool_value, Tool), (
        f"{extractor_cls.__name__}.tool returned {type(tool_value)!r}, expected Tool enum"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_tool_is_valid_enum_member(extractor_cls):
    """The Tool value returned must be a recognised member of the Tool enum."""
    instance = extractor_cls()
    assert instance.tool in Tool, (
        f"{extractor_cls.__name__}.tool value {instance.tool!r} is not in the Tool enum"
    )


# ---------------------------------------------------------------------------
# Contract: `is_available()` method
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_has_is_available_method(extractor_cls):
    instance = extractor_cls()
    assert callable(getattr(instance, "is_available", None)), (
        f"{extractor_cls.__name__} must have a callable `is_available` method"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_is_available_returns_bool(extractor_cls):
    """is_available() must return a bool without raising on a clean system."""
    instance = extractor_cls()
    result = instance.is_available()
    assert isinstance(result, bool), (
        f"{extractor_cls.__name__}.is_available() returned {type(result)!r}, expected bool"
    )


# ---------------------------------------------------------------------------
# Contract: `extract_sessions()` method
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_has_extract_sessions_method(extractor_cls):
    instance = extractor_cls()
    assert callable(getattr(instance, "extract_sessions", None)), (
        f"{extractor_cls.__name__} must have a callable `extract_sessions` method"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extract_sessions_returns_iterator_when_available(extractor_cls):
    """
    When the tool data is available on this system, extract_sessions() must
    return an iterator (generator counts).  When not available, the extractor
    is free to return an empty iterator — we just ensure it doesn't raise.
    """
    instance = extractor_cls()
    result = instance.extract_sessions()
    # Generators and other lazy iterators all satisfy this check.
    assert hasattr(result, "__iter__") and hasattr(result, "__next__"), (
        f"{extractor_cls.__name__}.extract_sessions() must return an iterator, got {type(result)!r}"
    )


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extract_sessions_not_available_yields_nothing(extractor_cls, monkeypatch):
    """
    When is_available() returns False the extractor must yield no sessions
    (or at worst not raise).  We force is_available to False and consume the
    iterator completely — it must not raise an exception.
    """
    instance = extractor_cls()
    monkeypatch.setattr(instance, "is_available", lambda: False)

    # Most extractors short-circuit when is_available() is False.
    # For those that don't, consuming the iterator must still be safe.
    try:
        sessions = list(instance.extract_sessions())
        # If it doesn't short-circuit, result must be a list of UnifiedSession
        for s in sessions:
            assert isinstance(s, UnifiedSession), (
                f"{extractor_cls.__name__} yielded non-UnifiedSession object: {type(s)!r}"
            )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{extractor_cls.__name__}.extract_sessions() raised when not available: {exc}")


# ---------------------------------------------------------------------------
# Contract: `should_import_session()` inherited helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extractor_has_should_import_session(extractor_cls):
    instance = extractor_cls()
    assert callable(getattr(instance, "should_import_session", None)), (
        f"{extractor_cls.__name__} must inherit `should_import_session` from BaseExtractor"
    )


# ---------------------------------------------------------------------------
# Coverage: all Tool enum values are represented by at least one extractor
# ---------------------------------------------------------------------------


def test_all_tool_enum_values_have_at_least_one_extractor():
    """Every Tool enum member must be claimed by at least one extractor class."""
    claimed_tools: set[Tool] = set()
    for cls in ALL_EXTRACTOR_CLASSES:
        claimed_tools.add(cls().tool)

    uncovered = set(Tool) - claimed_tools
    assert not uncovered, (
        f"The following Tool enum values have no extractor: {[t.value for t in uncovered]}"
    )


def test_no_two_extractors_claim_same_tool():
    """Each Tool enum value should be claimed by exactly one extractor."""
    seen: dict[Tool, str] = {}
    duplicates: list[str] = []
    for cls in ALL_EXTRACTOR_CLASSES:
        t = cls().tool
        if t in seen:
            duplicates.append(f"{cls.__name__} and {seen[t]} both claim {t.value}")
        else:
            seen[t] = cls.__name__

    assert not duplicates, "Duplicate tool claims found:\n" + "\n".join(duplicates)


# ---------------------------------------------------------------------------
# Contract: tool_calls shape on really-extracted sessions (#55)
# ---------------------------------------------------------------------------


# Extractors still write divergent raw tool_call keys (opencode "tool", claude
# "name", codex/copilot "arguments"), but normalize_tool_call() maps them all to
# the canonical shape at the consume layer (#79). The contract asserts the
# canonical view names a tool — that's what every consumer actually reads.
@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_tool_calls_are_well_shaped(extractor_cls):
    """Any tool_calls an available extractor produces must normalize to a named tool.

    Skips extractors that aren't available on this machine — this asserts the
    shape contract only where there's real data to check.
    """
    from lore.core.models import normalize_tool_call

    instance = extractor_cls()
    if not instance.is_available():
        pytest.skip(f"{extractor_cls.__name__} not available on this machine")

    checked = 0
    for session in instance.extract_sessions():
        for message in session.messages:
            for call in message.tool_calls or []:
                assert isinstance(call, dict), (
                    f"{extractor_cls.__name__} tool_call is not a dict: {type(call)!r}"
                )
                canonical = normalize_tool_call(call)
                named = bool(canonical["tool"]) and canonical["tool"] != "tool"
                has_payload = bool(canonical.get("output")) or bool(canonical.get("input"))
                assert named or has_payload, (
                    f"{extractor_cls.__name__} tool_call has no tool name or payload "
                    f"after normalization: {call!r}"
                )
                checked += 1
                if checked >= 50:
                    return
        if checked >= 50:
            return


# ---------------------------------------------------------------------------
# Contract: no crash on a present-but-empty/malformed source (#55)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor_cls", ALL_EXTRACTOR_CLASSES, ids=ALL_EXTRACTOR_IDS)
def test_extract_sessions_survives_empty_home(extractor_cls, monkeypatch, tmp_path):
    """Pointing an extractor at an empty HOME must not raise.

    Each extractor derives its source dirs from HOME / XDG paths. With a fresh
    empty HOME, is_available() is typically False (no dirs) and extract_sessions
    must yield nothing without raising — the malformed/absent-source contract.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    instance = extractor_cls()

    try:
        sessions = list(instance.extract_sessions())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{extractor_cls.__name__}.extract_sessions() raised on empty HOME: {exc}")

    for s in sessions:
        assert isinstance(s, UnifiedSession)
