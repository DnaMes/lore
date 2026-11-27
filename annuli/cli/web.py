"""``annuli-web`` entry point — serve the Annuli web UI.

This is a thin CLI wrapper around :func:`annuli.interfaces.web.start_web_ui`. It is
intended for local/dev use; in production the app is served by gunicorn
(``annuli.interfaces.web:app``) — see the Dockerfile.
"""

from __future__ import annotations

import argparse
import os
import sys


def _is_loopback_bind(host: str) -> bool:
    """Whether a bind host only accepts connections from this machine."""
    from annuli.interfaces.web_utils import is_loopback_host

    return is_loopback_host(host)


def warn_if_exposed_without_origin_config(host: str) -> None:
    """Warn loudly when binding beyond loopback with no origin hardening (#193).

    There is no authentication in ``annuli-web``: a non-loopback bind serves the
    UI and its state-changing endpoints to the network. Without
    ``ANNULI_PUBLIC_ORIGINS`` (reverse-proxy origin policy) or
    ``ANNULI_ALLOWED_HOSTS`` (Host allowlist) nothing constrains which Host/Origin
    may reach it. The warning goes to stderr so it is visible next to the
    server log even when logging is unconfigured.
    """
    if _is_loopback_bind(host):
        return
    if os.environ.get("ANNULI_PUBLIC_ORIGINS", "").strip():
        return
    if os.environ.get("ANNULI_ALLOWED_HOSTS", "").strip():
        return
    print(
        f"WARNING: annuli-web is bound to non-loopback host {host!r} with no origin "
        "hardening configured and no authentication exists: the UI (including "
        "state-changing endpoints) is reachable from other machines. Set "
        "ANNULI_PUBLIC_ORIGINS (behind a reverse proxy with TRUSTED_PROXY=1) or "
        "ANNULI_ALLOWED_HOSTS, or bind to 127.0.0.1 and use an SSH tunnel.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="annuli-web",
        description="Serve the Annuli web UI (Flask dev server). For production use gunicorn.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ANNULI_WEB_HOST", "127.0.0.1"),
        help="Bind host (default 127.0.0.1, or $ANNULI_WEB_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ANNULI_WEB_PORT", "5000")),
        help="Bind port (default 5000, or $ANNULI_WEB_PORT).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.environ.get("ANNULI_WEB_DEBUG", "").lower() == "true",
        help="Run Flask in debug mode (or set $ANNULI_WEB_DEBUG=true).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from annuli.utils.console import make_console_safe

    make_console_safe()
    args = build_parser().parse_args(argv)
    warn_if_exposed_without_origin_config(args.host)
    # Imported lazily so `--help` doesn't pay the heavy web.py import cost.
    from annuli.interfaces.web import start_web_ui

    start_web_ui(port=args.port, host=args.host, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
