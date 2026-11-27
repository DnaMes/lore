"""Runtime install hints must never point at PyPI ``lore``: that name belongs to an unrelated project."""

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "annuli"
BAD_HINT = re.compile(r"""pip install ['"]?lore\[""")


def test_no_runtime_hint_installs_pypi_lore():
    offenders = [
        f"{path.relative_to(PACKAGE.parent)}:{lineno}"
        for path in PACKAGE.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if BAD_HINT.search(line)
    ]
    assert offenders == []
