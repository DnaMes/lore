"""Clean-venv install-from-clone check for the documented console scripts (#196).

The README tells users to install from a clone (`pip install -e .`), so the
distribution itself must build and install out of this working tree and must
provide every documented entry point: `lore`, `lore-session`, `lore-web`,
`lore-mcp`, `lore-web-probe`. Import-only guards (see `test_cli_entry_points.py`)
never exercise the packaging metadata, which is how an entry point can be
declared in `pyproject.toml` while the module behind it is missing.

This builds a throwaway venv under `tmp_path` and installs the repository into
it, resolving dependencies like a first-time user would. It is deliberately
unguarded: a failed install is the bug this test exists to catch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONSOLE_SCRIPTS = ("lore", "lore-session", "lore-web", "lore-mcp", "lore-web-probe")


def _run(
    cmd: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def test_install_from_clone_provides_all_console_scripts(tmp_path):
    """A clean venv installed from this worktree exposes all five scripts."""
    venv_dir = tmp_path / "venv"
    created = _run([sys.executable, "-m", "venv", str(venv_dir)], timeout=300)
    assert created.returncode == 0, f"venv creation failed: {created.stderr[-2000:]}"

    bin_dir = venv_dir / ("Scripts" if (venv_dir / "Scripts").is_dir() else "bin")
    pip = bin_dir / "pip"
    assert pip.exists(), f"no pip in the fresh venv: {sorted(p.name for p in bin_dir.iterdir())}"

    # Run every subprocess outside the checkout: otherwise `cwd` lands on
    # sys.path and `import lore` would resolve to the repository, not the install.
    outside = tmp_path
    installed = _run([str(pip), "install", "--quiet", str(REPO)], timeout=900, cwd=outside)
    assert installed.returncode == 0, f"install from clone failed:\n{installed.stderr[-4000:]}"

    python = bin_dir / ("python.exe" if (bin_dir / "python.exe").exists() else "python")
    imported = _run(
        [str(python), "-c", "import lore, pathlib; print(pathlib.Path(lore.__file__))"],
        timeout=120,
        cwd=outside,
    )
    assert imported.returncode == 0, (
        f"`import lore` failed in the clean venv:\n{imported.stderr[-2000:]}"
    )
    # The installed copy must come from the venv, not from the repository checkout.
    assert str(venv_dir) in imported.stdout, (
        f"`lore` did not resolve to the venv: {imported.stdout.strip()}"
    )

    for script in CONSOLE_SCRIPTS:
        path = bin_dir / script
        assert path.exists(), f"missing console script: {script}"
        result = _run([str(path), "--help"], timeout=120, cwd=outside)
        assert result.returncode == 0, (
            f"{script} --help exited {result.returncode}:\n{result.stderr[-2000:]}"
        )
