"""P1b mechanical rename lore -> annuli in code/config files (run from the worktree root).

Usage: python3 rename_p1b.py [--dry-run] FILE...
Protected tokens stay verbatim: on-disk data formats, live host names renamed only in P3b,
browser storage keys (P1c adds a read fallback), and the explicit legacy shims.
"""

import re
import sys
from pathlib import Path

PROTECT = [
    r"\.lore-library\.json",
    r"lore-backup",
    r"lore-bundle",
    r"lore-portable-bundle",
    r"lore-sync",
    r"lore-app",
    r"lore-worker",
    r"stack-lore",
    r"lore\.local\.erdlabs\.com",
    r"lore\.(?:keys|peek|view)\.v\d",
    r"lore-memory",
    r"DnaMes/lore",
    r"\"\.lore\"",
    r"/\.lore/",
    r"\"LORE_\"",
    r"\bLORE_X\b",
]
PROTECT_RE = re.compile("|".join(PROTECT))
RULES = [
    (re.compile(r"(?<![A-Za-z0-9])(_*)lore(?![a-z0-9])"), r"\1annuli"),
    (re.compile(r"(?<![A-Za-z0-9])Lore(?![a-z0-9])"), "Annuli"),
    (re.compile(r"(?<![A-Za-z0-9])LORE(?![A-Z0-9])"), "ANNULI"),
]


def transform(text: str) -> str:
    out, pos = [], 0
    for match in PROTECT_RE.finditer(text):
        out.append(_apply(text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_apply(text[pos:]))
    return "".join(out)


def _apply(chunk: str) -> str:
    for pattern, repl in RULES:
        chunk = pattern.sub(repl, chunk)
    return chunk


def main(argv: list) -> int:
    dry = "--dry-run" in argv
    changed = 0
    for name in (a for a in argv if a != "--dry-run"):
        path = Path(name)
        old = path.read_text(encoding="utf-8")
        new = transform(old)
        if new != old:
            changed += 1
            if not dry:
                path.write_text(new, encoding="utf-8")
    print(f"{'would change' if dry else 'changed'} {changed} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    assert transform("explore folklore Lorem lore_home LORE_X _lore_x lore.cli loreKey") == (
        "explore folklore Lorem annuli_home LORE_X _annuli_x annuli.cli annuliKey"
    )
    assert transform('".lore" lore-sync.service lore-web') == '".lore" lore-sync.service annuli-web'
    sys.exit(main(sys.argv[1:]))
