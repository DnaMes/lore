#!/usr/bin/env python3
"""Smoke-test an installed Lore the way a first-time user would meet it.

Usage::

    python scripts/smoke_install.py --wheel dist/lore-X.Y.Z-py3-none-any.whl
    python scripts/smoke_install.py --pip-spec "lore==X.Y.Z" --extra semantic
    python scripts/smoke_install.py --installed    # lore* entry points on PATH (pipx)

Every lore subprocess gets HOME and the XDG/APPDATA roots inside a temp dir and runs
from a temp cwd, so neither the real home nor the source tree is touched. Output is one
line per check (PASS / FAIL / SKIP). The exit code is the number of FAIL checks, capped
at 255 (the OS limit).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

TIMEOUT = 120  # seconds for every subprocess
# A cold pip install (onnxruntime for [semantic], sdist builds on musl) can take minutes.
INSTALL_TIMEOUT = 900
WEB_WAIT = 30  # seconds for lore-web to answer /api/health
PROXY_URL = "http://127.0.0.1:9"  # nothing listens here: a send would fail loudly
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
LORE_SCRIPTS = ("lore", "lore-web", "lore-mcp", "lore-session")
IS_WINDOWS = os.name == "nt"
SESSION_ID = "smoke-long-02"
ZEBRA = "zebra"
MCP_MIN_TOOLS = 10
MCP_REQUIRED = ("search_history", "memory_write")
MCP_INIT = {"protocolVersion": "2024-11-05", "capabilities": {}}
MCP_INIT["clientInfo"] = {"name": "smoke-install", "version": "0"}
MCP_PAYLOAD = "".join(
    json.dumps(message) + "\n"
    for message in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": MCP_INIT},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
)
EMPTY_HOME_CMDS: list[tuple[list[str], tuple[str, ...]]] = [
    (["check"], ()),
    (["list"], ()),
    (["stats"], ("lore export --all",)),
    (["search", "foo"], ("lore export --all",)),
    (["export", "--all"], ()),
    (["reindex"], ()),
    (["sync", "claude-code"], ()),
]


@dataclass
class Result:
    step: str
    status: str  # PASS | FAIL | SKIP
    detail: str


@dataclass
class Ctx:
    exes: dict[str, str]  # script name -> path (only scripts that exist)
    python: str | None  # interpreter for the semantic import check
    from_venv: bool  # True when the venv was created here (pip is available)
    extra: str | None
    work: Path
    home: Path
    cwd: Path

    def exe(self, name: str) -> str:
        if name not in self.exes:
            raise RuntimeError(f"{name} not found (venv or PATH)")
        return self.exes[name]

    def run(
        self, name: str, *args: str, env: Mapping[str, str] | None = None, stdin: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return execute([self.exe(name), *args], self.home, self.cwd, env, stdin)


# ---------------------------------------------------------------- pure helpers


def isolated_env(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment that points every user-level root into ``home``.

    LORE_* and PYTHONPATH are dropped so developer overrides cannot reach the real
    ~/.lore or import the source tree.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith("LORE_") and k != "PYTHONPATH"}
    roots = {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_STATE_HOME": home / ".local" / "state",
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
    }
    env.update({k: str(v) for k, v in roots.items()})
    return env


def pick_free_port() -> int:
    """A loopback TCP port that was free when asked."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_headers(host: str, origin: str | None = None) -> dict[str, str]:
    """Request headers with an explicit Host and an optional Origin."""
    headers = {"Host": host, "Accept": "*/*", "User-Agent": "lore-smoke-install"}
    if origin is not None:
        headers["Origin"] = origin
    return headers


def bad_host_cases(port: int) -> list[str]:
    """Host values that must get 400, never 500."""
    return [
        f"attacker.example:{port}",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:\u00b2",  # superscript two: a non-ASCII digit
        "127.0.0.1:" + "9" * 4500,
    ]


def mcp_responses(stdout: str) -> dict[int, dict[str, Any]]:
    """JSON-RPC responses keyed by integer id; non-JSON lines are ignored."""
    found: dict[int, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("id"), int):
            found[message["id"]] = message
    return found


def mcp_tool_names(stdout: str) -> list[str]:
    """Tool names from the tools/list response (id 2)."""
    result = mcp_responses(stdout).get(2, {}).get("result")
    tools = result.get("tools", []) if isinstance(result, dict) else []
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    return [n for n in names if isinstance(n, str)]


def with_extra(target: str, extra: str) -> str:
    """``lore==1.0`` -> ``lore[extra]==1.0``; wheel paths get ``[extra]`` appended."""
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$", target)
    if target.endswith(".whl") or os.path.exists(target) or match is None:
        return f"{target}[{extra}]"
    return f"{match.group(1)}[{extra}]{match.group(2)}"


def shebang_python(path: str | None) -> str | None:
    """Interpreter named by a script's ``#!`` line (pipx venvs), else None."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            first = fh.readline(300).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    interpreter = first[2:].split()[0] if first[2:].split() else ""
    name = Path(interpreter).name
    return interpreter if name.startswith("python") else None  # skips "/usr/bin/env python3"


def summarize(results: Sequence[Result]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def exit_code(results: Sequence[Result]) -> int:
    return min(summarize(results)["FAIL"], 255)


def verdict(step: str, ok: bool, detail: str) -> Result:
    return Result(step, "PASS" if ok else "FAIL", detail)


def describe(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
    return (lines[-1] if lines else f"exit {proc.returncode}, no output")[:160]


def expect(
    step: str, proc: subprocess.CompletedProcess[str], rc: int = 0, needles: Sequence[str] = ()
) -> Result:
    out = proc.stdout + proc.stderr
    problems = []
    if proc.returncode != rc:
        problems.append(f"exit {proc.returncode}, want {rc}")
    if "Traceback" in out:
        problems.append("Traceback in output")
    missing = [n for n in needles if n not in out]
    if missing:
        problems.append(f"missing {missing!r}")
    if problems:
        return Result(step, "FAIL", "; ".join(problems) + " | " + describe(proc))
    return Result(step, "PASS", describe(proc))


def execute(
    argv: Sequence[str],
    home: Path,
    cwd: Path,
    extra_env: Mapping[str, str] | None = None,
    stdin: str = "",
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """One subprocess: isolated env, given cwd, stdin closed (or ``stdin``), 120 s default timeout."""
    env = isolated_env(home) | dict(extra_env or {})
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout or TIMEOUT,
        check=False,
    )


def http_request(
    port: int, method: str, path: str, host: str | None = None, origin: str | None = None
) -> tuple[int, bytes]:
    """HTTP call to 127.0.0.1; status 0 means the connection itself failed."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=build_headers(host or f"127.0.0.1:{port}", origin))
        resp = conn.getresponse()
        return resp.status, resp.read()
    except OSError:
        return 0, b""
    finally:
        conn.close()


# ---------------------------------------------------------------- steps


def step_version(ctx: Ctx) -> list[Result]:
    proc = ctx.run("lore", "--version")
    shape = re.fullmatch(r"lore \d+\.\d+\.\d+\S*", proc.stdout.strip())
    return [verdict("version", proc.returncode == 0 and shape is not None, describe(proc))]


def step_empty_home(ctx: Ctx) -> list[Result]:
    results = []
    for args, needles in EMPTY_HOME_CMDS:
        proc = ctx.run("lore", *args)
        results.append(expect(f"empty-home: lore {' '.join(args)}", proc, needles=needles))
    return results


def step_memory(ctx: Ctx) -> list[Result]:
    add_args = ["memory", "add", "--kind", "fact", "--title", "smoke", "--body", "probe"]
    add = ctx.run("lore", *add_args)
    found = re.search(r"#(\d+)", add.stdout)
    memory_id = found.group(1) if found else "1"
    results = [expect("memory: add", add, needles=("Stored memory",))]
    cmds = {
        "list": (["memory", "list"], ("probe",)),
        "search": (["memory", "search", "probe"], ("probe",)),
        "show": (["memory", "show", memory_id], ("probe",)),
        "context": (["memory", "context"], ("probe",)),
        "dedupe": (["memory", "dedupe"], ()),
    }
    for label, (args, needles) in cmds.items():
        proc = ctx.run("lore", *args)
        results.append(expect(f"memory: {label}", proc, needles=needles))
    return results


def step_privacy(ctx: Ctx) -> list[Result]:
    proxy_env = {k: PROXY_URL for k in PROXY_KEYS} | {"NO_PROXY": "", "no_proxy": ""}
    results = []
    for name in ("knowledge", "analyze"):
        proc = ctx.run("lore", name, env=proxy_env)
        results.append(expect(f"privacy: lore {name} without opt-in", proc, 2, ("allow-cloud",)))
    proc = ctx.run("lore", "improve", "--dry-run", env=proxy_env)
    results.append(expect("privacy: lore improve --dry-run", proc))
    return results


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _claude_records(session_id: str, prompts: list[str]) -> list[dict[str, Any]]:
    # Shape of tests/test_claude_extractor.py::_minimal_records.
    records: list[dict[str, Any]] = []
    for i, text in enumerate(prompts):
        base = {"sessionId": session_id, "timestamp": "2025-06-15T10:00:00"}
        records.append(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "uuid": f"{session_id}-u{i}",
                "version": "1.5.0",
                "gitBranch": "main",
                **base,
            }
        )
        records.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": f"answer {i}"},
                "uuid": f"{session_id}-a{i}",
                **base,
            }
        )
    return records


def _codex_records(session_id: str, prompts: list[str]) -> list[dict[str, Any]]:
    # Shape of tests/test_extractor_codex_enrichment.py (_meta and _user_import_gate).
    meta = {"id": session_id, "cli_version": "9.9.9", "cwd": "/smoke/codex-proj"}
    records: list[dict[str, Any]] = [
        {"type": "session_meta", "timestamp": "2025-06-15T10:00:00", "payload": meta}
    ]
    for text in prompts:
        message = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
        records.append(
            {"type": "response_item", "timestamp": "2025-06-15T10:00:30", "payload": message}
        )
    return records


def step_sessions(ctx: Ctx) -> list[Result]:
    projects = ctx.home / ".claude" / "projects" / "-smoke-zebra-project"
    short = projects / "short.jsonl"  # 1 prompt: below the default floor of 3, so skipped
    long_file = projects / "long.jsonl"  # 3 prompts: must be archived and listed
    _write_jsonl(short, _claude_records("smoke-short-01", ["just one prompt about apples"]))
    _write_jsonl(
        long_file, _claude_records(SESSION_ID, ["first prompt about zebra", "second", "third"])
    )
    codex = ctx.home / ".codex" / "sessions" / "2025" / "06" / "15" / "rollout-smoke.jsonl"
    _write_jsonl(codex, _codex_records("smoke-codex-03", ["codex one", "codex two", "codex three"]))

    export = ctx.run("lore", "export", "--all")
    skipped = re.search(r"Skipped (\d+) sessions? with fewer than", export.stdout + export.stderr)
    results = [
        verdict(
            "sessions: export --all skip count",
            export.returncode == 0 and skipped is not None,
            f"skipped {skipped.group(1)} short sessions" if skipped else describe(export),
        ),
        expect("sessions: list", ctx.run("lore", "list"), needles=(ZEBRA,)),
        expect(
            "sessions: search zebra",
            ctx.run("lore", "search", ZEBRA),
            needles=(ZEBRA,),
        ),
        expect(
            "sessions: stats",
            ctx.run("lore", "stats"),
            needles=("Total Sessions:",),
        ),
    ]
    rows_proc = ctx.run("lore", "list", "--format", "json")
    try:
        rows = json.loads(rows_proc.stdout)
    except ValueError as exc:
        rows = None
        results.append(verdict("sessions: list --format json", False, f"not JSON: {exc}"))
    if isinstance(rows, list):
        titles = [str(r.get("title", "")) for r in rows if isinstance(r, dict)]
        ok = rows_proc.returncode == 0 and any(ZEBRA in t for t in titles)
        results.append(
            verdict("sessions: list --format json", ok, f"{len(rows)} rows, zebra present")
        )

    long_file.unlink()  # archive preservation: the source is gone, the archive must remain
    rerun = ctx.run("lore", "export", "--all")
    results.append(expect("sessions: export after source deleted", rerun))
    again = ctx.run("lore", "search", ZEBRA)
    results.append(expect("sessions: search survives source deletion", again, needles=(ZEBRA,)))
    return results


def step_prune(ctx: Ctx) -> list[Result]:
    decoy = ctx.work / "outside" / "decoy.md"  # outside ctx.home on purpose
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("keep me", encoding="utf-8")

    index_file = ctx.home / ".lore" / "index.json"
    index = json.loads(index_file.read_text(encoding="utf-8"))
    entry = dict(index["sessions"][0])
    entry.pop("origin", None)  # automated sessions are never pruned by default
    entry.update(id="smoke-decoy", title="smoke-decoy", export_path=str(decoy), prompts=1)
    index["sessions"].append(entry)
    index_file.write_text(json.dumps(index), encoding="utf-8")

    dry = ctx.run("lore", "prune", "--dry-run", "--max-prompts", "1")
    real = ctx.run("lore", "prune", "--max-prompts", "1")
    kept = decoy.exists() and decoy.read_text(encoding="utf-8") == "keep me"
    return [
        expect("prune: --dry-run", dry),
        expect("prune: --max-prompts 1", real),
        verdict("prune: decoy outside home kept", kept, str(decoy)),
    ]


def _wait_health(port: int, proc: subprocess.Popen[bytes]) -> str:
    """Empty string once /api/health answers 200; otherwise the reason it did not."""
    deadline = time.monotonic() + WEB_WAIT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"lore-web exited early with code {proc.returncode}"
        if http_request(port, "GET", "/api/health")[0] == 200:
            return ""
        time.sleep(0.25)
    return f"no HTTP 200 from /api/health within {WEB_WAIT} s"


def _web_checks(port: int, proc: subprocess.Popen[bytes]) -> list[Result]:
    problem = _wait_health(port, proc)
    if problem:
        return [verdict("web: /api/health", False, problem)]
    results = [verdict("web: /api/health", True, f"127.0.0.1:{port} answers")]
    pages = [
        "/",
        "/sessions",
        "/projects",
        "/stats",
        f"/session/{SESSION_ID}",
        "/api/health",
        "/api/ready",
    ]
    html = ""
    for path in pages:
        status, body = http_request(port, "GET", path)
        if path == "/":
            html = body.decode("utf-8", "replace")
        results.append(verdict(f"web: GET {path}", status == 200, f"HTTP {status}"))
    asset = next(iter(re.findall(r'(?:href|src)="(/static/[^"]+)"', html)), None)
    if asset is None:
        results.append(verdict("web: static asset", False, "no /static/ link on /"))
    else:
        status, _ = http_request(port, "GET", asset)
        results.append(verdict(f"web: GET {asset}", status == 200, f"HTTP {status}"))
    for host in bad_host_cases(port):
        status, _ = http_request(port, "GET", "/api/health", host=host)
        shown = host if len(host) <= 40 else f"{host[:20]}...({len(host)} chars)"
        results.append(verdict(f"web: Host {shown}", status == 400, f"HTTP {status}"))
    # /api/cache/clear is a mutating route guarded by _check_local_origin in lore/interfaces/web.py.
    status, _ = http_request(port, "POST", "/api/cache/clear", origin="http://localhost:9999")
    results.append(
        verdict("web: POST /api/cache/clear foreign Origin", status == 403, f"HTTP {status}")
    )
    return results


def step_web(ctx: Ctx) -> list[Result]:
    port = pick_free_port()
    with (ctx.work / "lore-web.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [ctx.exe("lore-web"), "--host", "127.0.0.1", "--port", str(port)],
            cwd=ctx.cwd,
            env=isolated_env(ctx.home),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            results = _web_checks(port, proc)
        finally:
            if proc.poll() is None:  # the server child never outlives this step
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            closed = verdict("web: port closed after stop", False, f"127.0.0.1:{port} still open")
    except OSError:
        closed = verdict("web: port closed after stop", True, f"127.0.0.1:{port} refuses")
    results.append(closed)
    return results


def step_mcp(ctx: Ctx) -> list[Result]:
    proc = ctx.run("lore-mcp", stdin=MCP_PAYLOAD)
    init = mcp_responses(proc.stdout).get(1, {}).get("result")
    names = mcp_tool_names(proc.stdout)
    missing = [n for n in MCP_REQUIRED if n not in names]
    detail = f"{len(names)} tools" + (f", missing {missing}" if missing else "")
    server = (
        json.dumps(init.get("serverInfo", {})) if isinstance(init, dict) else proc.stderr[-120:]
    )
    return [
        verdict("mcp: initialize", isinstance(init, dict), f"serverInfo {server}"),
        verdict("mcp: tools/list", len(names) >= MCP_MIN_TOOLS and not missing, detail),
    ]


def step_semantic(ctx: Ctx) -> list[Result]:
    if not ctx.extra:
        return [Result("semantic: import", "SKIP", "--extra semantic not requested")]
    if not ctx.python:
        return [Result("semantic: import", "SKIP", "no interpreter for the installed scripts")]
    # Imports only: no model is downloaded.
    proc = execute([ctx.python, "-c", "import fastembed, sqlite_vec"], ctx.home, ctx.cwd)
    return [expect("semantic: import fastembed, sqlite_vec", proc)]


def step_packaging(ctx: Ctx) -> list[Result]:
    results = []
    if ctx.from_venv and ctx.python:
        # importlib.metadata instead of `pip show`: pip 25+ prints License-Expression, older
        # pips print License, and both come from the same core metadata.
        probe = (
            "import importlib.metadata as m; d = m.metadata('lore'); "
            "print(d.get('License-Expression') or d.get('License') or '')"
        )
        proc = execute([ctx.python, "-c", probe], ctx.home, ctx.cwd)
        has_license = proc.returncode == 0 and bool(proc.stdout.strip())
        ok = has_license
        results.append(
            verdict(
                "packaging: license metadata", ok, "License field present" if ok else describe(proc)
            )
        )
    else:
        results.append(
            Result("packaging: license metadata", "SKIP", "--installed: no venv pip to query")
        )
    proc = ctx.run("lore-session", "--help")
    results.append(expect("packaging: lore-session --help", proc))
    return results


# Order matters: privacy needs the index written by empty-home/export, and prune runs
# before web so the web checks see the post-prune archive.
STEPS: tuple[tuple[str, Callable[[Ctx], list[Result]]], ...] = (
    ("version", step_version),
    ("empty-home", step_empty_home),
    ("memory", step_memory),
    ("privacy", step_privacy),
    ("sessions", step_sessions),
    ("prune", step_prune),
    ("web", step_web),
    ("mcp", step_mcp),
    ("semantic", step_semantic),
    ("packaging", step_packaging),
)


def run_step(name: str, fn: Callable[[Ctx], list[Result]], ctx: Ctx) -> list[Result]:
    """Run one step; an exception becomes a FAIL so the remaining steps still run."""
    try:
        return fn(ctx)
    except Exception as exc:  # noqa: BLE001 - isolation boundary per step
        return [Result(name, "FAIL", f"{type(exc).__name__}: {exc}"[:200])]


# ---------------------------------------------------------------- setup and main


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test an installed Lore.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wheel", metavar="PATH", help="install this wheel into a fresh venv")
    mode.add_argument("--pip-spec", metavar="SPEC", help="install this spec into a fresh venv")
    mode.add_argument("--installed", action="store_true", help="use lore* entry points on PATH")
    parser.add_argument("--extra", choices=["semantic"], help="also install this extra")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the venv")
    parser.add_argument("--keep", action="store_true", help="keep the temp dir")
    parser.add_argument("--json", action="store_true", help="print a JSON list of results")
    return parser.parse_args(argv)


def _create_venv(
    args: argparse.Namespace, work: Path, home: Path, cwd: Path
) -> tuple[Ctx, list[Result]]:
    venv = work / "venv"
    made = execute([args.python, "-m", "venv", str(venv)], home, cwd)
    if made.returncode != 0:
        return Ctx({}, None, True, args.extra, work, home, cwd), [
            verdict("install: create venv", False, describe(made))
        ]
    bin_dir = venv / ("Scripts" if IS_WINDOWS else "bin")

    def script(name: str) -> str:
        return str(bin_dir / (f"{name}.exe" if IS_WINDOWS else name))

    python = script("python")
    target = str(Path(args.wheel).resolve()) if args.wheel else args.pip_spec
    if args.extra:
        target = with_extra(target, args.extra)
    install = execute([python, "-m", "pip", "install", target], home, cwd, timeout=INSTALL_TIMEOUT)
    exes = {n: script(n) for n in LORE_SCRIPTS if Path(script(n)).exists()}
    ctx = Ctx(exes, python, True, args.extra, work, home, cwd)
    return ctx, [verdict("install: pip install", install.returncode == 0, describe(install))]


def _installed_ctx(args: argparse.Namespace, work: Path, home: Path, cwd: Path) -> Ctx:
    found = {n: p for n in LORE_SCRIPTS if (p := shutil.which(n))}
    return Ctx(found, shebang_python(found.get("lore")), False, args.extra, work, home, cwd)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="lore-smoke-"))
    home, cwd = work / "home", work / "cwd"  # cwd is outside the source tree
    home.mkdir()
    cwd.mkdir()
    results: list[Result] = []

    def emit(result: Result) -> None:
        results.append(result)
        if not args.json:
            print(f"{result.status} {result.step}: {result.detail}", flush=True)

    try:
        if args.installed:
            ctx, setup = _installed_ctx(args, work, home, cwd), []
        else:
            ctx, setup = _create_venv(args, work, home, cwd)
        for result in setup:
            emit(result)
        for name, fn in STEPS:
            for result in run_step(name, fn, ctx):
                emit(result)
    except Exception as exc:  # noqa: BLE001 - report instead of a bare traceback
        emit(verdict("setup", False, f"{type(exc).__name__}: {exc}"))
    finally:
        if args.keep:
            print(f"kept temp dir: {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        counts = summarize(results)
        print(
            f"Summary: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped"
        )
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
