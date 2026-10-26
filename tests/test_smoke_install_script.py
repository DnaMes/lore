"""Unit tests for the pure helpers of scripts/smoke_install.py.

The end-to-end run (venv, pip install, lore subprocesses) lives in the script itself
and is never executed from the test suite; these tests only cover its helpers.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = ROOT / "scripts" / "smoke_install.py"
    spec = importlib.util.spec_from_file_location("smoke_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_install"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script()


def test_isolated_env_points_every_root_into_home(tmp_path):
    base = {"PATH": "/bin", "LORE_OUTPUT_DIR": "/real/.lore", "PYTHONPATH": "/src", "HOME": "/x"}
    env = smoke.isolated_env(tmp_path, base)
    for key in (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        assert env[key].startswith(str(tmp_path)), key
    assert env["PATH"] == "/bin"
    assert "LORE_OUTPUT_DIR" not in env
    assert "PYTHONPATH" not in env
    assert base["HOME"] == "/x"  # the base mapping is not mutated


def test_isolated_env_defaults_to_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    env = smoke.isolated_env(tmp_path)
    assert "LORE_ALLOW_CLOUD" not in env
    assert env["HOME"] == str(tmp_path)


def test_pick_free_port_returns_bindable_loopback_port():
    port = smoke.pick_free_port()
    assert 1 <= port <= 65535
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_build_headers_sets_host_and_optional_origin():
    headers = smoke.build_headers("127.0.0.1:5000")
    assert headers["Host"] == "127.0.0.1:5000"
    assert "Origin" not in headers
    headers = smoke.build_headers("127.0.0.1:5000", origin="http://localhost:9999")
    assert headers["Origin"] == "http://localhost:9999"


def test_bad_host_cases_cover_required_forms():
    cases = smoke.bad_host_cases(5000)
    assert "attacker.example:5000" in cases
    assert "127.0.0.1:0" in cases
    assert "127.0.0.1:65536" in cases
    assert "127.0.0.1:\u00b2" in cases
    assert any(len(c) > 4500 and c.endswith("9" * 4500) for c in cases)


def test_with_extra_handles_wheel_and_requirement(tmp_path):
    assert smoke.with_extra("lore", "semantic") == "lore[semantic]"
    assert smoke.with_extra("lore==2.4.0", "semantic") == "lore[semantic]==2.4.0"
    wheel = str(tmp_path / "lore-2.4.0-py3-none-any.whl")
    assert smoke.with_extra(wheel, "semantic") == wheel + "[semantic]"


def test_mcp_tool_names_skips_noise_and_reads_id_2():
    stdout = "\n".join(
        [
            "log line that is not JSON",
            '{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "lore"}}}',
            '{"jsonrpc": "2.0", "id": 2, "result": {"tools": ['
            '{"name": "search_history"}, {"name": "memory_write"}, {"description": "no name"}'
            "]}}",
        ]
    )
    assert smoke.mcp_tool_names(stdout) == ["search_history", "memory_write"]


def test_mcp_tool_names_empty_without_tools_list():
    assert smoke.mcp_tool_names('{"jsonrpc": "2.0", "id": 1, "result": {}}') == []
    assert smoke.mcp_tool_names("") == []


def test_summarize_and_exit_code_count_only_failures():
    R = smoke.Result
    results = [R("a", "PASS", ""), R("b", "FAIL", ""), R("c", "SKIP", ""), R("d", "FAIL", "")]
    assert smoke.summarize(results) == {"PASS": 1, "FAIL": 2, "SKIP": 1}
    assert smoke.exit_code(results) == 2


def test_exit_code_zero_on_success_and_capped_at_255():
    R = smoke.Result
    assert smoke.exit_code([R("a", "PASS", ""), R("b", "SKIP", "")]) == 0
    assert smoke.exit_code([R("a", "FAIL", "")] * 300) == 255


def test_expect_fails_on_wrong_exit_missing_needle_or_traceback():
    def proc(rc: int, out: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["lore"], rc, stdout=out, stderr="")

    assert smoke.expect("ok", proc(0, "hint: lore export --all"), needles=("export",)).status == (
        "PASS"
    )
    assert smoke.expect("rc", proc(1, "x")).status == "FAIL"
    assert smoke.expect("needle", proc(0, "x"), needles=("probe",)).status == "FAIL"
    traced = smoke.expect("tb", proc(0, "Traceback (most recent call last):"))
    assert traced.status == "FAIL" and "Traceback" in traced.detail


def test_shebang_python_reads_interpreter_but_not_env_wrappers(tmp_path):
    pipx = tmp_path / "lore"
    pipx.write_text("#!/home/u/.local/pipx/venvs/lore/bin/python3\nprint(1)\n", encoding="utf-8")
    env_wrapper = tmp_path / "other"
    env_wrapper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert smoke.shebang_python(str(pipx)) == "/home/u/.local/pipx/venvs/lore/bin/python3"
    assert smoke.shebang_python(str(env_wrapper)) is None
    assert smoke.shebang_python(str(tmp_path / "missing")) is None
    assert smoke.shebang_python(None) is None


def test_parse_args_requires_exactly_one_mode():
    assert smoke.parse_args(["--installed"]).installed is True
    assert smoke.parse_args(["--wheel", "x.whl", "--extra", "semantic"]).extra == "semantic"
    with pytest.raises(SystemExit):
        smoke.parse_args([])
    with pytest.raises(SystemExit):
        smoke.parse_args(["--installed", "--wheel", "x.whl"])


def test_isolated_env_drops_source_overrides_and_credentials(tmp_path):
    base = {
        "OPENCODE_STORAGE_PATHS": "/real/opencode",
        "OPENCODE_SQLITE_PATHS": "/real/opencode.db",
        "GEMINI_PROJECT_ROOTS": "/real/gemini",
        "CLAUDE_CONFIG_DIR": "/real/claude",
        "GEMINI_API_KEY": "secret",
        "OPENROUTER_API_KEY": "secret",
        "OLLAMA_HOST": "http://192.0.2.1:11434",
        "KEEP_ME": "yes",
    }
    env = smoke.isolated_env(tmp_path, base)
    assert not (set(base) - {"KEEP_ME"}) & set(env)
    assert env["KEEP_ME"] == "yes"
