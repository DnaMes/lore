"""The native-CLI provider boundary (#180) and its adapters (#181, #182, #183, #184).

Every test here runs **fake executables** written to a tmp directory and put first
on ``PATH``: no real CLI is called and nothing touches the network. The fakes
record argv, the inherited environment and the prompt they received, so the
command/environment boundary is asserted rather than assumed.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lore.llm.adapters import (
    CREDENTIAL_ENV_VARS,
    AdapterAuthError,
    AdapterCancelled,
    AdapterExecutionError,
    AdapterOutputError,
    AdapterPromptTooLarge,
    AdapterTimeout,
    AdapterUnavailable,
    CancellationToken,
    ExecutionPolicy,
    UnknownProviderError,
    UnsupportedProviderCombination,
    select_provider,
)
from lore.llm.adapters.claude import ClaudeCliAdapter
from lore.llm.adapters.codex import CodexCliAdapter
from lore.llm.adapters.gemini import GeminiCliAdapter
from lore.llm.factory import get_provider

ADAPTER_PROVIDERS = ("codex-cli", "claude-cli", "gemini-cli", "antigravity-cli")
FAKE_CLI = (
    "#!{python}\n"
    '"""Fake native CLI: records its inputs, then emits the documented payload."""\n'
    "import json\n"
    "import os\n"
    "import sys\n"
    "import time\n"
    "\n"
    "name = os.path.basename(sys.argv[0])\n"
    "argv = sys.argv[1:]\n"
    "log_path = os.environ.get('FAKE_CLI_LOG')\n"
    "mode = os.environ.get('FAKE_CLI_MODE', 'success')\n"
    "help_mode = os.environ.get('FAKE_CLI_HELP_MODE', 'full')\n"
    "\n"
    "HELP = {\n"
    "    'codex': 'Usage: codex exec [--json] [--sandbox MODE]',\n"
    "    'claude': 'Usage: claude --print --output-format json --model NAME',\n"
    "    'gemini': 'Usage: gemini -p PROMPT --output-format json --model NAME',\n"
    "}\n"
    "LIMITED = {'codex': 'Usage: codex exec', 'claude': 'Usage: claude --print',\n"
    "           'gemini': 'Usage: gemini -p PROMPT'}\n"
    "\n"
    "def record(extra):\n"
    "    if not log_path:\n"
    "        return\n"
    "    entry = {'name': name, 'argv': argv, 'env': dict(os.environ)}\n"
    "    entry.update(extra)\n"
    "    with open(log_path, 'a', encoding='utf-8') as handle:\n"
    "        handle.write(json.dumps(entry) + '\\n')\n"
    "\n"
    "if '--help' in argv:\n"
    "    sys.stdout.write((LIMITED if help_mode == 'limited' else HELP).get(name, 'usage') + '\\n')\n"
    "    record({'kind': 'help'})\n"
    "    sys.exit(0)\n"
    "\n"
    "if '--version' in argv:\n"
    "    sys.stdout.write('1.4.2 (fake ' + name + ')\\n')\n"
    "    record({'kind': 'version'})\n"
    "    sys.exit(0)\n"
    "\n"
    "prompt = sys.stdin.read()\n"
    "record({'kind': 'run', 'stdin': prompt})\n"
    "\n"
    "if mode == 'slow':\n"
    "    time.sleep(30)\n"
    "    sys.exit(0)\n"
    "if mode == 'auth':\n"
    "    sys.stderr.write('Error: not logged in. Run `' + name + ' login` first.\\n')\n"
    "    sys.exit(1)\n"
    "if mode == 'exit':\n"
    "    sys.stderr.write('boom: sandbox refused\\n')\n"
    "    sys.exit(7)\n"
    "if mode == 'malformed':\n"
    "    sys.stdout.write('{not json\\n')\n"
    "    sys.exit(0)\n"
    "if mode == 'empty':\n"
    "    sys.exit(0)\n"
    "\n"
    "content = 'fake answer for ' + name\n"
    "if mode == 'partial':\n"
    "    sys.stdout.write(json.dumps({'type': 'item.completed', 'item': {'item_type':\n"
    "        'agent_message', 'text': content}}) + '\\n')\n"
    '    sys.stdout.write(\'{"type": "turn.completed", "usage": {"input_tok\')\n'
    "    sys.exit(0)\n"
    "if name == 'codex':\n"
    "    sys.stdout.write(json.dumps({'type': 'thread.started'}) + '\\n')\n"
    "    sys.stdout.write(json.dumps({'type': 'item.completed', 'item': {'item_type':\n"
    "        'agent_message', 'text': content}}) + '\\n')\n"
    "    sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage':\n"
    "        {'input_tokens': 3, 'output_tokens': 5}}) + '\\n')\n"
    "elif name == 'claude':\n"
    "    sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success',\n"
    "        'is_error': False, 'result': content, 'session_id': 's-1', 'num_turns': 1,\n"
    "        'total_cost_usd': 0.01, 'usage': {'input_tokens': 4, 'output_tokens': 6}}) + '\\n')\n"
    "elif name == 'gemini':\n"
    "    sys.stdout.write(json.dumps({'response': content,\n"
    "        'stats': {'models': {'m': {'tokens': {'total': 11}}}}}) + '\\n')\n"
    "else:\n"
    "    sys.stdout.write(content + '\\n')\n"
)


@pytest.fixture()
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp bin directory first on PATH, plus the fake's call log."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)
    monkeypatch.delenv("FAKE_CLI_HELP_MODE", raising=False)
    return bin_dir


@pytest.fixture()
def empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH with nothing on it: the CLIs under test cannot exist here."""
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "calls.jsonl"))
    return bin_dir


def install(fake_bin: Path, name: str) -> Path:
    script = fake_bin / name
    script.write_text(FAKE_CLI.replace("{python}", sys.executable))
    script.chmod(0o755)
    return script


def install_all(fake_bin: Path) -> None:
    for name in ("codex", "claude", "gemini", "antigravity"):
        install(fake_bin, name)


def calls(tmp_path: Path) -> List[Dict[str, Any]]:
    log = tmp_path / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _adapter(provider: str, **kwargs: Any):
    kwargs.setdefault("timeout", 10)
    return get_provider(provider, allow_cloud=True, **kwargs)


# --- availability and detection ---------------------------------------------


@pytest.mark.parametrize("provider", ADAPTER_PROVIDERS)
def test_missing_cli_is_unavailable_with_a_reason_and_never_a_fallback(
    provider: str, empty_path: Path, tmp_path: Path
) -> None:
    adapter = _adapter(provider)
    assert adapter.is_available() is False
    probe = adapter.availability()
    assert probe.available is False
    assert probe.reason
    with pytest.raises(AdapterUnavailable) as exc_info:
        adapter.generate("summarise this")
    assert adapter.availability().executable in str(exc_info.value)
    assert exc_info.value.reason
    # Nothing ran: no CLI, and no API provider was constructed in its place.
    assert calls(tmp_path) == []
    assert type(adapter).__name__ not in ("GeminiProvider", "OpenRouterProvider", "JevProvider")


def test_version_and_capabilities_come_from_the_installed_cli(
    fake_bin: Path, tmp_path: Path
) -> None:
    install_all(fake_bin)
    probe = _adapter("codex-cli").probe()
    assert probe.available is True
    assert probe.version == "1.4.2"
    assert str(fake_bin) in (probe.path or "")
    assert probe.capabilities == ("--json",)
    kinds = [call["kind"] for call in calls(tmp_path)]
    assert kinds[:2] == ["version", "help"]


def test_an_installed_cli_without_the_documented_flag_is_unavailable(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_HELP_MODE", "limited")
    adapter = _adapter("codex-cli")
    assert adapter.is_available() is False
    probe = adapter.availability()
    assert "--json" in probe.reason
    assert probe.version == "1.4.2"  # reported, not hidden, even when unusable
    with pytest.raises(AdapterUnavailable, match="--json"):
        adapter.generate("hi")


def test_a_cli_that_cannot_report_its_version_is_unavailable(fake_bin: Path) -> None:
    script = fake_bin / "claude"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(0o755)
    adapter = _adapter("claude-cli")
    assert adapter.is_available() is False
    assert "exited 3" in adapter.availability().reason


# --- bounded execution -------------------------------------------------------


def test_timeout_kills_the_call_and_is_reported_as_a_timeout(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    adapter = _adapter("claude-cli", timeout=0.4)
    started = time.monotonic()
    with pytest.raises(AdapterTimeout) as exc_info:
        adapter.generate("slow prompt")
    assert exc_info.value.timeout == 0.4
    assert time.monotonic() - started < 10


def test_cancellation_kills_the_call_and_is_reported_as_cancelled(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    token = CancellationToken()
    adapter = _adapter("codex-cli", timeout=30, cancellation=token)
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(AdapterCancelled):
        adapter.generate("cancel me")
    assert time.monotonic() - started < 10


def test_an_already_cancelled_token_never_starts_the_cli(fake_bin: Path, tmp_path: Path) -> None:
    install_all(fake_bin)
    token = CancellationToken()
    token.cancel()
    adapter = _adapter("gemini-cli", cancellation=token)
    with pytest.raises(AdapterCancelled):
        adapter.generate("never runs")
    assert [call["kind"] for call in calls(tmp_path)] == ["version", "help"]


def test_prompt_bound_is_enforced_from_the_execution_policy(fake_bin: Path) -> None:
    install_all(fake_bin)
    adapter = _adapter("claude-cli")
    adapter.request = replace(
        adapter.request,
        policy=ExecutionPolicy(
            executable="claude",
            max_prompt_chars=10,
        ),
    )
    with pytest.raises(AdapterPromptTooLarge):
        adapter.generate("this prompt is longer than ten characters")


# --- structured output -------------------------------------------------------

SUCCESS_CONTENT = {
    "codex-cli": "fake answer for codex",
    "claude-cli": "fake answer for claude",
    "gemini-cli": "fake answer for gemini",
}


@pytest.mark.parametrize("provider", sorted(SUCCESS_CONTENT))
def test_documented_structured_output_is_parsed(
    provider: str, fake_bin: Path, tmp_path: Path
) -> None:
    install_all(fake_bin)
    response = _adapter(provider).generate("prompt")
    assert response.content == SUCCESS_CONTENT[provider]
    assert response.cached is False
    assert response.metadata["auth_mode"] == "native-cli"
    assert response.metadata["cli_version"] == "1.4.2"
    assert response.metadata["adapter"] == provider
    assert response.tokens_used > 0


@pytest.mark.parametrize("provider", sorted(SUCCESS_CONTENT))
def test_malformed_output_is_an_output_error(
    provider: str, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "malformed")
    with pytest.raises(AdapterOutputError):
        _adapter(provider).generate("prompt")


@pytest.mark.parametrize("provider", sorted(SUCCESS_CONTENT))
def test_empty_output_is_an_output_error(
    provider: str, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "empty")
    with pytest.raises(AdapterOutputError):
        _adapter(provider).generate("prompt")


def test_partial_jsonl_keeps_the_complete_agent_message(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "partial")
    response = _adapter("codex-cli").generate("prompt")
    assert response.content == "fake answer for codex"
    assert response.metadata["partial"] is True


def test_codex_accepts_the_legacy_event_shape() -> None:
    from lore.llm.base import LLMConfig

    adapter = CodexCliAdapter(LLMConfig(provider="codex-cli", model=""))
    raw = json.dumps({"msg": {"type": "agent_message", "message": "legacy answer"}})
    assert adapter.parse_output(raw).content == "legacy answer"


@pytest.mark.parametrize("provider", sorted(SUCCESS_CONTENT))
def test_non_zero_exit_names_the_exit_code(
    provider: str, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "exit")
    with pytest.raises(AdapterExecutionError) as exc_info:
        _adapter(provider).generate("prompt")
    assert exc_info.value.returncode == 7
    assert "boom" in str(exc_info.value)


@pytest.mark.parametrize("provider", sorted(SUCCESS_CONTENT))
def test_a_refused_login_is_an_auth_error(
    provider: str, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    monkeypatch.setenv("FAKE_CLI_MODE", "auth")
    with pytest.raises(AdapterAuthError, match="not logged in"):
        _adapter(provider).generate("prompt")


# --- command and environment boundary ---------------------------------------


def test_the_prompt_travels_on_stdin_and_never_in_argv(fake_bin: Path, tmp_path: Path) -> None:
    install_all(fake_bin)
    secret = "SENTINEL-PROMPT-TEXT"
    _adapter("claude-cli", model="sonnet").generate(f"analyse {secret}")
    run = [call for call in calls(tmp_path) if call["kind"] == "run"][-1]
    assert secret in run["stdin"]
    assert all(secret not in arg for arg in run["argv"])
    assert run["argv"] == ["--print", "--output-format", "json", "--model", "sonnet"]


def test_credential_variables_are_not_inherited_by_the_cli(
    fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    for name in sorted(CREDENTIAL_ENV_VARS):
        monkeypatch.setenv(name, f"token-for-{name}")
    _adapter("codex-cli").generate("prompt")
    env = [call for call in calls(tmp_path) if call["kind"] == "run"][-1]["env"]
    leaked = sorted(name for name in CREDENTIAL_ENV_VARS if name in env)
    assert leaked == []
    assert "FAKE_CLI_LOG" in env  # the user's own environment otherwise survives


def test_credential_files_are_never_read(
    fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_all(fake_bin)
    home = Path(os.environ["HOME"])
    planted = [
        home / ".codex" / "auth.json",
        home / ".claude" / ".credentials.json",
        home / ".gemini" / "oauth_creds.json",
    ]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"refresh_token": "PLANTED-CREDENTIAL"}))
    opened: List[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    for provider in ("codex-cli", "claude-cli", "gemini-cli"):
        _adapter(provider).generate("prompt")

    assert [path for path in opened if str(home) in path] == []
    for call in calls(tmp_path):
        assert "PLANTED-CREDENTIAL" not in json.dumps(call)


# --- selection ---------------------------------------------------------------


def test_unknown_provider_is_a_typed_value_error() -> None:
    with pytest.raises(UnknownProviderError, match="Unknown provider"):
        get_provider("no-such-provider")
    assert issubclass(UnknownProviderError, ValueError)


def test_an_api_key_is_refused_for_a_native_cli() -> None:
    with pytest.raises(UnsupportedProviderCombination, match="own login"):
        get_provider("claude-cli", api_key="sk-not-a-cli-credential", allow_cloud=True)
    assert issubclass(UnsupportedProviderCombination, ValueError)


def test_a_mismatched_auth_mode_is_refused() -> None:
    with pytest.raises(UnsupportedProviderCombination, match="not supported"):
        get_provider("gemini", auth_mode="native-cli", allow_cloud=True)
    with pytest.raises(UnsupportedProviderCombination, match="not supported"):
        get_provider("codex-cli", auth_mode="api-key", allow_cloud=True)


def test_a_base_url_is_refused_for_a_subprocess_provider() -> None:
    with pytest.raises(UnsupportedProviderCombination, match="base_url"):
        get_provider("gemini-cli", base_url="http://localhost:1", allow_cloud=True)


def test_non_cli_providers_keep_their_contract() -> None:
    gemini = get_provider("gemini", allow_cloud=True)
    assert gemini.config.model == "gemini-2.0-flash"
    assert gemini.config.timeout == 60
    assert gemini.provider_identity()["provider"] == "GeminiProvider"
    ollama = get_provider("ollama")
    assert ollama.config.model == "llama3.2"
    # An in-process provider has no executable, so no subprocess contract at all.
    assert select_provider("ollama").policy.in_process is True
    assert select_provider("gemini").policy.in_process is True


def test_factory_routes_every_adapter_name_to_its_adapter(fake_bin: Path) -> None:
    install_all(fake_bin)
    expected = {
        "codex-cli": CodexCliAdapter,
        "claude-cli": ClaudeCliAdapter,
        "gemini-cli": GeminiCliAdapter,
    }
    for name, cls in expected.items():
        adapter = _adapter(name)
        assert isinstance(adapter, cls)
        assert adapter.request.provider == name
        assert adapter.request.auth_mode.value == "native-cli"
        assert adapter.request.policy.executable == cls.EXECUTABLE


def test_a_missing_cli_never_becomes_an_api_provider(
    empty_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lore.llm import factory

    def _boom(*_args, **_kwargs):
        raise AssertionError("no API provider may be built for a native-CLI request")

    monkeypatch.setattr(factory, "GeminiProvider", _boom)
    monkeypatch.setattr(factory, "OpenRouterProvider", _boom)
    monkeypatch.setattr(factory, "JevProvider", _boom)
    adapter = _adapter("codex-cli")
    assert adapter.is_available() is False
    with pytest.raises(AdapterUnavailable):
        adapter.generate("prompt")
    assert calls(tmp_path) == []


# --- the request descriptor --------------------------------------------------


def test_request_descriptor_round_trips_with_every_field() -> None:
    token = CancellationToken()
    request = select_provider(
        "claude-cli",
        model="sonnet",
        timeout=42,
        cancellation=token,
        task="summarization",
        prompt_version="v3",
        cache_scope="session:abc@r2",
    )
    restored = type(request).from_json(request.to_json())
    assert restored == request
    assert restored.to_dict() == request.to_dict()
    assert restored.cancellation.cancelled is False
    assert restored.model == "sonnet"
    assert restored.timeout == 42
    assert restored.task == "summarization"
    assert restored.prompt_version == "v3"
    assert restored.cache_scope == "session:abc@r2"
    assert restored.auth_mode.value == "native-cli"
    assert restored.policy.output_format.value == "json"
    assert restored.policy.prompt_transport.value == "stdin"


def test_a_persisted_cancelled_token_stays_cancelled() -> None:
    token = CancellationToken()
    token.cancel()
    request = select_provider("codex-cli", cancellation=token)
    assert request.to_dict()["cancellation"] == {"cancellable": True, "cancelled": True}
    assert type(request).from_json(request.to_json()).cancellation.cancelled is True


def test_cache_identity_covers_the_request_and_the_cli(fake_bin: Path) -> None:
    install_all(fake_bin)
    claude = _adapter("claude-cli")
    codex = _adapter("codex-cli")
    assert claude.provider_identity()["version"] == "1.4.2"
    assert claude.provider_identity()["auth_mode"] == "native-cli"
    assert claude._get_cache_key("p", "s") != codex._get_cache_key("p", "s")
    identity = claude.cache_identity("p", "s")
    assert identity["request"]["auth_mode"] == "native-cli"
    assert identity["request"]["cli_version"] == "1.4.2"
    assert identity["request"]["policy"]["executable"] == "claude"


# --- the API provider keeps its own credentials ------------------------------


def test_gemini_api_provider_does_not_read_cli_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lore.llm.base import LLMConfig
    from lore.llm.gemini import GeminiProvider

    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    home = Path(os.environ["HOME"])
    creds = home / ".gemini" / "oauth_creds.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"refresh_token": "PLANTED-CREDENTIAL"}))
    opened: List[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    provider = GeminiProvider(LLMConfig(provider="gemini", model="gemini-2.0-flash"))
    with pytest.raises(ValueError, match="authentication not found") as exc_info:
        provider._get_client()
    assert str(creds) not in opened
    # The refusal points at the CLI adapter instead of borrowing the CLI's login.
    assert "gemini-cli" in str(exc_info.value)


# --- Antigravity (#183) ------------------------------------------------------


def test_antigravity_is_unavailable_without_a_binary(empty_path: Path, tmp_path: Path) -> None:
    adapter = _adapter("antigravity-cli")
    assert adapter.is_available() is False
    assert "antigravity" in adapter.availability().reason
    with pytest.raises(AdapterUnavailable, match="antigravity"):
        adapter.generate("anything")
    assert calls(tmp_path) == []


def test_antigravity_stays_unavailable_with_a_binary_and_runs_nothing(
    fake_bin: Path, tmp_path: Path
) -> None:
    install_all(fake_bin)
    adapter = _adapter("antigravity-cli")
    assert adapter.is_available() is False
    assert "contract" in adapter.availability().reason
    with pytest.raises(AdapterUnavailable, match="contract"):
        adapter.generate("anything")
    # No version probe, no help probe, no invocation.
    assert calls(tmp_path) == []
