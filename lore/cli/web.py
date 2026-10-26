"""``lore-web`` entry point — serve the Lore web UI.

This is a thin CLI wrapper around :func:`lore.interfaces.web.start_web_ui`. It is
intended for local/dev use; in production the app is served by gunicorn
(``lore.interfaces.web:app``) — see the Dockerfile.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys


def _is_loopback_bind(host: str) -> bool:
    """Whether a bind host only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def warn_if_exposed_without_origin_config(host: str) -> None:
    """Warn loudly when binding beyond loopback with no origin hardening (#193).

    There is no authentication in ``lore-web``: a non-loopback bind serves the
    UI and its state-changing endpoints to the network. Without
    ``LORE_PUBLIC_ORIGINS`` (reverse-proxy origin policy) or
    ``LORE_ALLOWED_HOSTS`` (Host allowlist) nothing constrains which Host/Origin
    may reach it. The warning goes to stderr so it is visible next to the
    server log even when logging is unconfigured.
    """
    if _is_loopback_bind(host):
        return
    if os.environ.get("LORE_PUBLIC_ORIGINS", "").strip():
        return
    if os.environ.get("LORE_ALLOWED_HOSTS", "").strip():
        return
    print(
        f"WARNING: lore-web is bound to non-loopback host {host!r} with no origin "
        "hardening configured and no authentication exists: the UI (including "
        "state-changing endpoints) is reachable from other machines. Set "
        "LORE_PUBLIC_ORIGINS (behind a reverse proxy with TRUSTED_PROXY=1) or "
        "LORE_ALLOWED_HOSTS, or bind to 127.0.0.1 and use an SSH tunnel.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lore-web",
        description="Serve the Lore web UI (Flask dev server). For production use gunicorn.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LORE_WEB_HOST", "127.0.0.1"),
        help="Bind host (default 127.0.0.1, or $LORE_WEB_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LORE_WEB_PORT", "5000")),
        help="Bind port (default 5000, or $LORE_WEB_PORT).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.environ.get("LORE_WEB_DEBUG", "").lower() == "true",
        help="Run Flask in debug mode (or set $LORE_WEB_DEBUG=true).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from lore.utils.console import make_console_safe

    make_console_safe()
    args = build_parser().parse_args(argv)
    warn_if_exposed_without_origin_config(args.host)
    # Imported lazily so `--help` doesn't pay the heavy web.py import cost.
    from lore.interfaces.web import start_web_ui

    start_web_ui(port=args.port, host=args.host, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
