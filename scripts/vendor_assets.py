#!/usr/bin/env python3
"""Download and verify the vendored web assets (highlight.js).

The web UI loads no third-party CDNs so it works in air-gapped installs
(issue #19). This script (re-)downloads the pinned asset versions into
``lore/interfaces/static/`` and verifies their SHA-256 hashes against
the manifest below.

Tailwind is NOT downloaded here anymore: since issue #129 the UI ships a
build-time compiled stylesheet (``tailwind-compiled.min.css``) produced by
``scripts/build_tailwind.sh`` — a build artifact, not a vendored runtime.

Usage:
    python scripts/vendor_assets.py           # verify checked-in assets
    python scripts/vendor_assets.py --download # re-download then verify

Run with ``--download`` only when bumping a pinned version; commit both the
new file and the updated hash in this manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "lore" / "interfaces" / "static"

# filename -> (download URL, expected SHA-256)
ASSETS: dict[str, tuple[str, str]] = {
    "highlight-11.9.0.min.js": (
        "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js",
        "837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846",
    ),
    "highlight-github-11.9.0.min.css": (
        "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css",
        "3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066",
    ),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_assets() -> None:
    """Fetch each asset from its pinned URL into the static directory."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, _) in ASSETS.items():
        print(f"downloading {name} <- {url}")
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (pinned HTTPS URLs)
            data = resp.read()
        (STATIC_DIR / name).write_bytes(data)
        print(f"  {len(data):,} bytes, sha256={_sha256(data)}")


def verify_assets() -> bool:
    """Return True if every checked-in asset matches its manifest hash."""
    ok = True
    for name, (_, expected) in ASSETS.items():
        path = STATIC_DIR / name
        if not path.exists():
            print(f"MISSING: {path}", file=sys.stderr)
            ok = False
            continue
        actual = _sha256(path.read_bytes())
        if actual != expected:
            print(
                f"HASH MISMATCH: {name}\n  expected {expected}\n  actual   {actual}",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"ok: {name}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download",
        action="store_true",
        help="re-download assets before verifying (use when bumping versions)",
    )
    args = parser.parse_args()

    if args.download:
        download_assets()

    if not verify_assets():
        print("asset verification FAILED", file=sys.stderr)
        return 1
    print("all vendored assets verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
