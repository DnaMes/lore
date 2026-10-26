"""Utility functions for web interface.

This module contains:
- Rate limiting utilities
- Metrics tracking
- Noise rules management
- Request ID generation
"""

import ipaddress
import json
import logging
import os
import time
import uuid
from threading import Lock
from typing import Optional, Tuple
from urllib.parse import urlparse

from flask import request

# ActionJobCancelledError is defined in the framework-free service layer
# (issue #47) so both ``services`` and the interface modules share one
# exception type. Re-exported here for backwards compatibility — callers
# and tests historically import it from ``web_utils``.
from lore.services import ActionJobCancelledError  # noqa: F401

from ..utils.paths import lore_home

logger = logging.getLogger(__name__)

# Rate limiting state
RATE_LIMIT_STATE: dict[tuple[str, str], list[float]] = {}
RATE_LIMIT_LOCK = Lock()
RATE_LIMIT_BUCKET_TTL_SECONDS = 3600

# Metrics state
METRICS: dict[str, int] = {}
METRICS_LOCK = Lock()

# Paths
OUTPUT_DIR = lore_home()

# Origins the app is legitimately served from, as a comma-separated list of
# ``scheme://host[:port]`` (#193). Honoured only while TRUSTED_PROXY is set —
# without a proxy nothing rewrites scheme/host, so the loopback check below is
# all a direct deployment needs.
LORE_PUBLIC_ORIGINS_ENV = "LORE_PUBLIC_ORIGINS"

# Reverse-proxy trust switch. Only these values (case-insensitive, surrounding
# whitespace ignored) enable it; unset, empty, ``0``, ``false``, ``no``, ``off``
# and anything else leave it disabled.
TRUSTED_PROXY_ENV = "TRUSTED_PROXY"
_TRUSTED_PROXY_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def trusted_proxy_enabled() -> bool:
    """Whether ``TRUSTED_PROXY`` is set to a true value (read per call)."""
    raw = (os.environ.get(TRUSTED_PROXY_ENV) or "").strip().lower()
    return raw in _TRUSTED_PROXY_TRUE_VALUES


# Hosts a browser (and the Docker healthcheck) can only mean for "this machine":
# always a permitted ``Host`` header. ``LOOPBACK_HOSTS`` in Host-header spelling
# — an IPv6 literal needs brackets to parse as a host.
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")

# Extra permitted ``Host`` values, comma separated (#193 follow-up). The literal
# ``*`` allows every Host, but only when set explicitly here.
LORE_ALLOWED_HOSTS_ENV = "LORE_ALLOWED_HOSTS"

# Bind hosts that listen on every interface; they never appear as a Host value.
WILDCARD_BIND_HOSTS = ("0.0.0.0", "::")

# ``LORE_ALLOWED_HOSTS='*'`` turns Host validation off entirely; complain about
# that exactly once per process instead of once per request.
_WILDCARD_HOSTS_WARNED = False

# Hosts a browser can only mean for "this machine": always a permitted Origin.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _parse_port(raw: str) -> Optional[int]:
    """Parse a TCP port as ``int`` in ``1..65535``; ``None`` when invalid.

    Never raises — a 4500-digit string, ``0``, ``65536``, or non-ASCII digits
    such as ``²`` or fullwidth ``１２`` are all ``None`` so Host/Origin checks
    reject them as not-allowed instead of 500-ing.
    """
    if not raw or len(raw) > 5 or not raw.isascii() or not raw.isdigit():
        return None
    port = int(raw)
    if 1 <= port <= 65535:
        return port
    return None


def parse_origin(origin: str) -> Optional[Tuple[str, str, Optional[int]]]:
    """Split an Origin into ``(scheme, host, port)``; ``None`` when malformed.

    Exception-proof wrapper: any malformed or non-ASCII input is ``None``.
    """
    if not isinstance(origin, str) or not origin.isascii() or not origin.isprintable():
        return None
    try:
        return _parse_origin(origin)
    except (ValueError, TypeError):
        return None


def _parse_origin(origin: str) -> Optional[Tuple[str, str, Optional[int]]]:
    """Split an Origin into ``(scheme, host, port)``; ``None`` when it is malformed.

    A browser Origin is exactly ``scheme://host[:port]``: a real scheme, a host,
    and no path, query, fragment or userinfo. Everything else (``javascript:``,
    ``https://``, a userinfo form, a non-numeric / out-of-range port) is
    malformed and must never be treated as an origin (#193).
    """
    raw = (origin or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    # Bound the port ourselves — ``parsed.port`` raises on huge values and
    # accepts ``0``, which must not normalise equal to a portless host.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    port: Optional[int] = None
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None
        rest = authority[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port = _parse_port(rest[1:])
            if port is None:
                return None
    elif authority.count(":") == 1:
        _host, _sep, port_s = authority.rpartition(":")
        port = _parse_port(port_s)
        if port is None:
            return None
    elif authority.count(":") > 1:
        return None  # bare IPv6 without brackets is not a valid Origin host
    return parsed.scheme.lower(), host.lower(), port


def normalize_origin(scheme: str, host: str, port: Optional[int]) -> str:
    """Canonical origin: lowercase scheme and host, default port omitted."""
    hostname = f"[{host}]" if ":" in host else host  # bracket IPv6 literals
    base = f"{scheme}://{hostname}"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{base}:{port}"
    return base


def configured_public_origins() -> Tuple[str, ...]:
    """Normalized origins from ``LORE_PUBLIC_ORIGINS``.

    Entries that are not ``scheme://host[:port]`` are dropped with one warning
    instead of being silently accepted or failing the whole list (#193).
    """
    raw = os.environ.get(LORE_PUBLIC_ORIGINS_ENV, "")
    origins: list[str] = []
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        parsed = parse_origin(entry)
        if parsed is None:
            logger.warning(
                "Ignoring malformed entry in %s: %r", LORE_PUBLIC_ORIGINS_ENV, entry[:120]
            )
            continue
        origins.append(normalize_origin(*parsed))
    return tuple(origins)


def _split_host(value: str) -> Optional[Tuple[str, Optional[int]]]:
    """Split a ``host[:port]`` value into ``(hostname, port)``; ``None`` when malformed.

    Exception-proof wrapper around :func:`_split_host_checked`; non-ASCII input
    (IDN hostnames, non-ASCII digits) is rejected explicitly.
    """
    if not isinstance(value, str) or not value.isascii() or not value.isprintable():
        return None
    try:
        return _split_host_checked(value)
    except (ValueError, TypeError):
        return None


def _split_host_checked(value: str) -> Optional[Tuple[str, Optional[int]]]:
    """Split a ``host[:port]`` value into ``(hostname, port)``; ``None`` when malformed.

    Accepts dotted hostnames and bracketed IPv6 literals with the same rules
    the ``X-Forwarded-Host`` validation from #193 uses, so the Host allowlist
    and the proxy checks agree on what a well-formed host is. Ports must be
    integers in ``1..65535`` (via :func:`_parse_port`); ``0`` / ``65536`` /
    absurd digit strings are malformed, never an exception.
    """
    raw = (value or "").strip()
    if not raw or any(c in raw for c in " /\\@?#%"):
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        try:
            ipaddress.IPv6Address(raw[1:end])
        except ValueError:
            return None
        rest = raw[end + 1 :]
        if not rest:
            return raw[1:end].lower(), None
        if not rest.startswith(":"):
            return None
        port = _parse_port(rest[1:])
        if port is None:
            return None
        return raw[1:end].lower(), port
    # hostname or IPv4, optional ``:port`` — parse the port ourselves so a
    # 4500-digit suffix cannot raise via ``urlparse(...).port``.
    host_part, sep, port_s = raw.rpartition(":")
    port: Optional[int] = None
    if sep and host_part and host_part.count(":") == 0:
        port = _parse_port(port_s)
        if port is None:
            return None
        raw = host_part
    elif sep:
        # ``host:port:extra`` (or bare IPv6) — not a valid Host spelling.
        return None
    try:
        parsed = urlparse(f"http://{raw}")
    except ValueError:
        return None
    if parsed.hostname is None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return parsed.hostname, port


def request_origin() -> str:
    """Own origin of the current request: ``scheme://host[:port]``.

    Built from the connection scheme and the ``Host`` header — both rewritten
    by ``ProxyFix`` while ``TRUSTED_PROXY`` is set — so a browser's same-origin
    ``Origin`` header compares exactly against it. Malformed ``X-Forwarded-*``
    headers are rejected in ``before_request`` before this runs (#193).
    """
    host_header = request.headers.get("Host", "")
    split = _split_host(host_header)
    host, port = split if split is not None else (host_header.strip().lower(), None)
    return normalize_origin(request.scheme, host, port)


def _is_valid_host(value: str) -> bool:
    """A ``host[:port]`` header value: dotted labels or a bracketed IPv6 literal."""
    split = _split_host(value)
    if split is None:
        return False
    hostname = split[0]
    if ":" in hostname:  # bracketed IPv6 literal — already validated by _split_host
        return True
    for label in hostname.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return False
        if not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label):
            return False
    return True


def _is_valid_forwarded_for(hop: str) -> bool:
    """One ``X-Forwarded-For`` hop: an IP, ``ip:port`` or ``[v6]:port``."""
    hop = hop.strip()
    if not hop:
        return False
    if hop.startswith("["):
        end = hop.find("]")
        if end == -1:
            return False
        try:
            ipaddress.IPv6Address(hop[1:end])
        except ValueError:
            return False
        rest = hop[end + 1 :]
        if not rest:
            return True
        return rest.startswith(":") and _parse_port(rest[1:]) is not None
    host, sep, port = hop.rpartition(":")
    if sep and host.count(":") == 0:
        if _parse_port(port) is None:
            return False
        hop = host  # IPv4:port
    try:
        ipaddress.ip_address(hop)
    except ValueError:
        return False
    return True


def forwarded_header_error() -> Optional[str]:
    """Reason to reject a request whose ``X-Forwarded-*`` headers are unusable (#193).

    Returns ``None`` when no trusted proxy is configured — the headers are then
    ignored entirely (never parsed, never trusted) — and when every header that
    is present is well-formed. Checked on state-changing routes, which is where
    a spoofed scheme/host would buy an attacker something.
    """
    if not trusted_proxy_enabled():
        return None

    proto = request.headers.get("X-Forwarded-Proto")
    if proto is not None:
        values = [p.strip().lower() for p in proto.split(",")]
        if not values or any(v not in ("http", "https") for v in values):
            return f"X-Forwarded-Proto must list only http/https: {proto[:60]!r}"

    host = request.headers.get("X-Forwarded-Host")
    if host is not None:
        first = host.split(",")[0].strip()
        if not _is_valid_host(first):
            return f"X-Forwarded-Host is not a valid host[:port]: {host[:60]!r}"

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for is not None:
        hops = forwarded_for.split(",")
        if any(not _is_valid_forwarded_for(h) for h in hops):
            return f"X-Forwarded-For is not a list of addresses: {forwarded_for[:60]!r}"

    return None


def configured_allowed_hosts(bind_host: str = "") -> Tuple[Tuple[str, Optional[int]], ...]:
    """``(hostname, port)`` pairs a request's ``Host`` header may carry.

    Always includes the loopback defaults (``localhost``, ``127.0.0.1``,
    ``[::1]``), the configured bind host (``start_web_ui`` passes it; gunicorn
    deployments rely on ``LORE_WEB_HOST``), and the host of every
    ``LORE_PUBLIC_ORIGINS`` entry — an operator who configured a public origin
    means that host to reach the app (#193). ``LORE_ALLOWED_HOSTS`` (comma
    separated) adds more entries; the literal ``*`` allows every Host, but only
    when set explicitly there. An entry without a port matches any port; an
    entry with a port requires the same port.
    """
    global _WILDCARD_HOSTS_WARNED
    allowed: set[tuple[str, Optional[int]]] = set()

    def add(entry: str) -> None:
        split = _split_host(entry)
        if split is None:
            logger.warning("Ignoring malformed host entry: %r", entry[:120])
            return
        allowed.add(split)

    for entry in DEFAULT_ALLOWED_HOSTS:
        add(entry)
    bind = (bind_host or os.environ.get("LORE_WEB_HOST", "")).strip()
    if bind and bind not in WILDCARD_BIND_HOSTS:
        add(bind)
    for origin in configured_public_origins():
        parsed = parse_origin(origin)
        if parsed is not None:
            allowed.add((parsed[1], parsed[2]))
    for chunk in os.environ.get(LORE_ALLOWED_HOSTS_ENV, "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        if entry == "*":
            if not _WILDCARD_HOSTS_WARNED:
                _WILDCARD_HOSTS_WARNED = True
                logger.warning(
                    "%s='*' disables Host header validation entirely; any Host is accepted",
                    LORE_ALLOWED_HOSTS_ENV,
                )
            allowed.add(("*", None))
        else:
            add(entry)
    return tuple(allowed)


def host_header_allowed(host_header: str, bind_host: str = "") -> bool:
    """Whether a ``Host`` header passes :func:`configured_allowed_hosts`."""
    allowed = configured_allowed_hosts(bind_host)
    if ("*", None) in allowed:
        return True
    split = _split_host(host_header)
    if split is None:
        return False
    host, port = split
    return any(h == host and (p is None or p == port) for h, p in allowed)


NOISE_RULES_PATH = OUTPUT_DIR / "noise_rules.json"


class ActionJobTimeoutError(RuntimeError):
    pass


def _rate_limit_enabled() -> bool:
    raw = (os.environ.get("LORE_RATE_LIMIT_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


def _rate_limit_window_seconds() -> int:
    return _env_int("LORE_RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1)


def _rate_limit_max_for_path(path: str) -> int:
    normalized = path.strip()
    if normalized == "/api/reload-sessions":
        return _env_int("LORE_RATE_LIMIT_RELOAD_PER_WINDOW", 12, minimum=1)
    if normalized == "/api/audit":
        return _env_int("LORE_RATE_LIMIT_AUDIT_PER_WINDOW", 20, minimum=1)
    if normalized == "/api/search":
        return _env_int("LORE_RATE_LIMIT_SEARCH_PER_WINDOW", 180, minimum=1)
    return _env_int("LORE_RATE_LIMIT_MAX_API_REQUESTS", 240, minimum=1)


def _client_ip() -> str:
    # Only read X-Forwarded-For / X-Real-IP when running behind a trusted proxy.
    # If the app is exposed directly, these headers can be spoofed by any client.
    # Set TRUSTED_PROXY=1 (or true/yes/on) when a reverse proxy is in front.
    if trusted_proxy_enabled():
        forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip
    return request.remote_addr or "unknown"


def _should_rate_limit(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return path not in {"/api/build-info", "/api/health", "/api/ready", "/api/live"}


def _consume_rate_limit(
    path: str, client_ip: str, now_epoch: Optional[float] = None
) -> tuple[bool, int, int, int, int]:
    now = now_epoch if now_epoch is not None else time.time()
    window = _rate_limit_window_seconds()
    max_requests = _rate_limit_max_for_path(path)
    cutoff = now - window
    stale_cutoff = now - RATE_LIMIT_BUCKET_TTL_SECONDS
    key = (client_ip, path)

    with RATE_LIMIT_LOCK:
        stale_keys: list[tuple[str, str]] = []
        for bucket_key, bucket in RATE_LIMIT_STATE.items():
            fresh = [stamp for stamp in bucket if stamp >= stale_cutoff]
            if fresh:
                RATE_LIMIT_STATE[bucket_key] = fresh
            else:
                stale_keys.append(bucket_key)
        for bucket_key in stale_keys:
            RATE_LIMIT_STATE.pop(bucket_key, None)

        bucket = [stamp for stamp in RATE_LIMIT_STATE.get(key, []) if stamp >= cutoff]
        if len(bucket) >= max_requests:
            oldest = bucket[0]
            retry_after = max(1, int(window - (now - oldest)))
            RATE_LIMIT_STATE[key] = bucket
            reset_in = retry_after
            return False, retry_after, 0, max_requests, reset_in

        bucket.append(now)
        RATE_LIMIT_STATE[key] = bucket
        remaining = max(0, max_requests - len(bucket))
        reset_in = max(1, int(window - (now - bucket[0]))) if bucket else window
        return True, 0, remaining, max_requests, reset_in


def _request_id() -> str:
    value = (request.headers.get("X-Request-ID") or "").strip()
    if not value:
        value = f"req-{uuid.uuid4().hex[:16]}"
    return value[:128]


def _metrics_inc(key: str, amount: int = 1) -> None:
    with METRICS_LOCK:
        METRICS[key] = int(METRICS.get(key, 0)) + int(amount)


def _metrics_snapshot() -> dict[str, int]:
    with METRICS_LOCK:
        return {k: int(v) for k, v in METRICS.items()}


def _record_job_outcome(kind: str, status: str, started_epoch: float) -> None:
    duration_ms = max(0, int((time.time() - started_epoch) * 1000))
    prefix = "reload" if kind == "reload" else "audit"

    _metrics_inc(f"{prefix}_jobs_duration_ms_total", duration_ms)
    if status == "completed":
        _metrics_inc(f"{prefix}_jobs_completed")
    elif status == "cancelled":
        _metrics_inc(f"{prefix}_jobs_cancelled")
    else:
        _metrics_inc(f"{prefix}_jobs_failed")


def _json_request_logging_enabled() -> bool:
    raw = (os.environ.get("LORE_JSON_REQUEST_LOGS") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _default_noise_rules() -> dict:
    return {
        "default": {
            "strip_ansi_sequences": True,
        },
        "claude-code": {
            "claude_strip_command_xml": True,
            "claude_summarize_file_operations": True,
        },
        "cursor": {
            "cursor_summarize_file_operations": True,
        },
        "gemini-cli": {
            "gemini_summarize_file_operations": True,
        },
        "codex": {
            "codex_summarize_file_operations": True,
        },
        "copilot-cli": {
            "copilot_summarize_file_operations": True,
        },
        "vscode-copilot": {
            "vscode_summarize_file_operations": True,
        },
        "opencode": {
            "opencode_strip_user_caveat": True,
            "opencode_summarize_file_operations": True,
            "opencode_summarize_unset_env_vars": True,
        },
        "antigravity": {
            "antigravity_summarize_file_operations": True,
        },
    }


def _noise_rule_presets() -> dict:
    balanced = _default_noise_rules()

    aggressive = _default_noise_rules()
    for provider_rules in aggressive.values():
        if not isinstance(provider_rules, dict):
            continue
        for key in list(provider_rules.keys()):
            provider_rules[key] = True

    minimal = _default_noise_rules()
    for provider, provider_rules in minimal.items():
        if not isinstance(provider_rules, dict):
            continue
        for key in list(provider_rules.keys()):
            provider_rules[key] = provider == "default" and key == "strip_ansi_sequences"

    return {
        "balanced": {
            "label": "Balanced",
            "description": "Recommended default cleanup",
            "rules": balanced,
        },
        "aggressive": {
            "label": "Aggressive Cleanup",
            "description": "Hide most repetitive operational noise",
            "rules": aggressive,
        },
        "minimal": {
            "label": "Minimal Cleanup",
            "description": "Keep transcript mostly raw",
            "rules": minimal,
        },
    }


def load_noise_rules() -> dict:
    defaults = _default_noise_rules()
    if not NOISE_RULES_PATH.exists():
        return defaults
    try:
        with open(NOISE_RULES_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("Failed to load noise rules from %s: %s", NOISE_RULES_PATH, exc)
        return defaults

    if not isinstance(data, dict):
        return defaults

    merged = _default_noise_rules()
    for provider, rules in data.items():
        if not isinstance(provider, str) or not isinstance(rules, dict):
            continue
        dst = merged.setdefault(provider, {})
        for key, value in rules.items():
            if isinstance(key, str) and isinstance(value, bool):
                dst[key] = value
    return merged


def save_noise_rules(payload: dict) -> dict:
    current = _default_noise_rules()
    if isinstance(payload, dict):
        for provider, rules in payload.items():
            if not isinstance(provider, str) or not isinstance(rules, dict):
                continue
            dst = current.setdefault(provider, {})
            for key, value in rules.items():
                if isinstance(key, str) and isinstance(value, bool):
                    dst[key] = value

    NOISE_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOISE_RULES_PATH, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, sort_keys=True)
    return current
