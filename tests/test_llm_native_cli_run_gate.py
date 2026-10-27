"""Regression: every native-CLI adapter gates ``run()`` on cloud opt-in.

The code-review probe that started this file: an adapter built with
``allow_cloud=False`` reached the transport through ``run()`` while the same call
through ``generate()`` raised :class:`CloudOptInRequired` (the gate only lived in
``generate()``). The gate now sits at the top of ``NativeCliAdapter.run()``, so
nothing — not even the ``--version`` probe — starts without opt-in.

No real CLI is called here: ``probe`` is stubbed with an available ``CliProbe``
and ``_invoke`` (the transport) is replaced by a recorder. Local-model adapters
are unaffected — the gate only applies to these cloud native-CLI adapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from lore.llm import CloudOptInRequired, LLMConfig
from lore.llm.adapters import (
    AdapterUnavailable,
    AntigravityCliAdapter,
    ClaudeCliAdapter,
    CliProbe,
    CodexCliAdapter,
    GeminiCliAdapter,
)

# adapter class -> provider name and a stdout its parse_output accepts
ADAPTERS = {
    CodexCliAdapter: "codex-cli",
    ClaudeCliAdapter: "claude-cli",
    GeminiCliAdapter: "gemini-cli",
    AntigravityCliAdapter: "antigravity-cli",
}

EXECUTABLES: Dict[str, str] = {
    "codex-cli": "codex",
    "claude-cli": "claude",
    "gemini-cli": "gemini",
    "antigravity-cli": "antigravity",
}

FAKE_OUTPUTS: Dict[str, str] = {
    "codex-cli": json.dumps(
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": "ok"}}
    ),
    "claude-cli": json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
    ),
    "gemini-cli": json.dumps({"response": "ok"}),
    "antigravity-cli": "unused: the availability stub reports unavailable",
}


def _build(adapter_cls: type, executable: str, allow_cloud: bool):
    """A directly constructed adapter with a probe that reports the CLI available."""
    adapter = adapter_cls(
        LLMConfig(provider=ADAPTERS[adapter_cls], model="", timeout=10),
        allow_cloud=allow_cloud,
    )
    adapter.probe = lambda: CliProbe(  # type: ignore[method-assign]
        adapter.PROVIDER, executable, True, path=f"/fake-bin/{executable}", version="9.9.9"
    )
    return adapter


def _patched(argvs: List[List[str]], stdout: str) -> Tuple[str, str]:
    """The transport recorder: stand in for ``_invoke`` and count its calls."""

    def _invoke(fake_argv: List[str], fake_payload: str) -> Tuple[str, str]:
        argvs.append(list(fake_argv))
        return stdout, ""

    return _invoke  # type: ignore[return-value]


@pytest.mark.parametrize("adapter_cls", list(ADAPTERS))
def test_run_without_opt_in_raises_and_never_reaches_the_transport(
    adapter_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = EXECUTABLES[ADAPTERS[adapter_cls]]
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    adapter = _build(adapter_cls, executable, allow_cloud=False)
    argvs: List[List[str]] = []
    adapter._invoke = _patched(argvs, FAKE_OUTPUTS[ADAPTERS[adapter_cls]])  # type: ignore[method-assign]

    with pytest.raises(CloudOptInRequired) as exc_info:
        adapter.run("summarise this")
    assert adapter.PROVIDER in str(exc_info.value)
    assert argvs == []  # the mocked transport was called 0 times


@pytest.mark.parametrize(
    "adapter_cls", [cls for cls in ADAPTERS if cls is not AntigravityCliAdapter]
)
def test_run_with_opt_in_reaches_the_transport_once(
    adapter_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = EXECUTABLES[ADAPTERS[adapter_cls]]
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    adapter = _build(adapter_cls, executable, allow_cloud=True)
    argvs: List[List[str]] = []
    adapter._invoke = _patched(argvs, FAKE_OUTPUTS[ADAPTERS[adapter_cls]])  # type: ignore[method-assign]

    result = adapter.run("summarise this")
    assert result.content == "ok"
    assert len(argvs) == 1  # the mocked transport was called once


def test_run_with_opt_in_for_antigravity_is_still_availability_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented stub (#183) reports unavailable; the transport is never invoked."""
    monkeypatch.setenv("PATH", str(tmp_path))  # no antigravity executable installed
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    adapter = AntigravityCliAdapter(
        LLMConfig(provider="antigravity-cli", model="", timeout=10), allow_cloud=True
    )
    argvs: List[List[str]] = []

    def _invoke(fake_argv: List[str], fake_payload: str) -> Any:
        argvs.append(list(fake_argv))
        return "", ""

    adapter._invoke = _invoke  # type: ignore[method-assign]
    with pytest.raises(AdapterUnavailable, match="antigravity"):
        adapter.run("summarise this")
    assert argvs == []


@pytest.mark.parametrize("adapter_cls", list(ADAPTERS))
def test_run_honours_the_lore_allow_cloud_environment_override(
    adapter_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LORE_ALLOW_CLOUD=1`` opts in for ``run()`` exactly as it does for ``generate()``."""
    executable = EXECUTABLES[ADAPTERS[adapter_cls]]
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    adapter = _build(adapter_cls, executable, allow_cloud=False)
    if adapter_cls is AntigravityCliAdapter:
        pytest.skip("antigravity's availability stub blocks before any invocation")
    argvs: List[List[str]] = []
    adapter._invoke = _patched(argvs, FAKE_OUTPUTS[ADAPTERS[adapter_cls]])  # type: ignore[method-assign]

    result = adapter.run("summarise this")
    assert result.content == "ok"
    assert len(argvs) == 1
