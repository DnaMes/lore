"""CLI orchestration for ``lore summarize`` (B4.4, #197)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from argparse import Namespace
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from lore.services.summarize import (
    SessionEstimate,
    SummarizeReport,
    estimate_run,
    run_summaries,
    select_candidates,
)
from lore.storage.writer import v2_db_path
from lore.utils.datetime import parse_duration
from lore.utils.tooling import normalize_tool_name

LOCAL_PROVIDERS = frozenset({"ollama"})
PROVIDER_CHOICES = ("gemini", "ollama", "openrouter")
_LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_SINCE_NO_SESSION = "30d"
MCP_SUMMARIZE_PROVIDER = "ollama"


class SessionIdResolutionError(ValueError):
    """Unknown or ambiguous ``--session`` id/prefix."""


def list_indexed_session_ids(output_dir: Path) -> list[str]:
    """Session ids from index.json, or from the v2 store when the index is absent."""
    index_path = output_dir / "index.json"
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        return [str(entry["id"]) for entry in index.get("sessions", []) if entry.get("id")]

    db = v2_db_path(output_dir)
    if not db.exists():
        return []
    from lore.storage import open_connection

    conn = open_connection(db)
    try:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
        return [str(row["id"]) for row in rows]
    finally:
        conn.close()


def resolve_session_id(output_dir: Path, query: str) -> str:
    """Resolve a full session id or unique prefix against indexed ids."""
    ids = list_indexed_session_ids(output_dir)
    if query in ids:
        return query
    matches = [sid for sid in ids if sid.startswith(query)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SessionIdResolutionError(
            f"Ambiguous session id '{query}' matches {len(matches)} sessions"
        )
    raise SessionIdResolutionError(f"No session found for id '{query}'")


def provider_locality(provider: str) -> str:
    """ "local" only for a local provider whose actual endpoint is loopback.

    An Ollama client honours ``OLLAMA_HOST``: pointing it off-host makes it a remote
    endpoint that must be treated (and announced) like any other cloud provider.
    """
    name = provider.lower()
    if name not in LOCAL_PROVIDERS:
        return "cloud"
    if name == "ollama" and not llm_base_url_is_local(resolve_ollama_host()):
        return "cloud"
    return "local"


def llm_base_url_is_local(base_url: str | None) -> bool:
    """True when the LLM endpoint hostname is loopback (or unset)."""
    if base_url is None or not str(base_url).strip():
        return True
    raw = str(base_url).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    return _host_is_loopback(host)


def _host_is_loopback(host: str) -> bool:
    """Loopback by address, or by name only when EVERY resolved address is loopback."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if host not in _LOCAL_LLM_HOSTS:
        return False  # arbitrary hostnames are never trusted as local
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in addresses)
    except ValueError:
        return False


def resolve_ollama_host() -> str | None:
    return os.environ.get("OLLAMA_HOST")


def mcp_summarize_model() -> str:
    return os.environ.get("LORE_SUMMARY_MODEL") or default_model(MCP_SUMMARIZE_PROVIDER)


def check_mcp_summarize_locality() -> str | None:
    """Return an error message when MCP summarize cannot use a local endpoint."""
    if not llm_base_url_is_local(resolve_ollama_host()):
        return "Refused off-host Ollama base_url; MCP summarize requires a local provider"
    return None


def web_summarize_enabled() -> bool:
    """True when the web UI may offer local session summary generation."""
    provider = os.environ.get("LORE_SUMMARY_PROVIDER", "").strip().lower()
    if provider != MCP_SUMMARIZE_PROVIDER:
        return False
    if provider_locality(MCP_SUMMARIZE_PROVIDER) != "local":
        return False
    return check_mcp_summarize_locality() is None


def web_summarize_disabled_reason() -> str:
    """Short user-facing hint when :func:`web_summarize_enabled` is false."""
    provider = os.environ.get("LORE_SUMMARY_PROVIDER", "").strip().lower()
    if provider != MCP_SUMMARIZE_PROVIDER:
        return "No local provider configured: set LORE_SUMMARY_PROVIDER=ollama"
    if provider_locality(MCP_SUMMARIZE_PROVIDER) != "local":
        return "Ollama endpoint must be loopback (check OLLAMA_HOST)"
    err = check_mcp_summarize_locality()
    return err or "Local summarize provider is not available"


def build_mcp_summarize_provider():
    """Construct a transcript-safe local provider for MCP ``summarize_session``."""
    from lore.llm import LLMConfig, get_provider
    from lore.llm.base import transcript_safe

    locality_err = check_mcp_summarize_locality()
    if locality_err:
        raise ValueError(locality_err)
    model = mcp_summarize_model()
    config = LLMConfig.for_transcripts(provider=MCP_SUMMARIZE_PROVIDER, model=model)
    provider = get_provider(provider=config.provider, model=config.model)
    model_label = f"{MCP_SUMMARIZE_PROVIDER}/{model}"
    return transcript_safe(provider), model_label


def default_model(provider: str) -> str:
    if provider == "gemini":
        return "gemini-2.0-flash"
    if provider == "openrouter":
        from lore.llm.openrouter import DEFAULT_MODEL

        return os.environ.get("LORE_OPENROUTER_MODEL") or DEFAULT_MODEL
    return "llama3.2"


def _estimate_totals(estimates: list[SessionEstimate]) -> dict[str, int]:
    return {
        "compressed_tokens": sum(e.compressed_tokens for e in estimates),
        "chunks": sum(e.chunks for e in estimates),
        "expected_calls": sum(e.expected_calls for e in estimates),
        "input_tokens": sum(e.input_tokens for e in estimates),
        "output_tokens": sum(e.output_tokens for e in estimates),
    }


def _print_dry_run_table(estimates: list[SessionEstimate]) -> None:
    headers = (
        "session_id",
        "status",
        "compressed_tokens",
        "chunks",
        "expected_calls",
        "input_tokens",
        "output_tokens",
    )
    print("\t".join(headers))
    for row in estimates:
        print(
            "\t".join(
                str(getattr(row, name))
                for name in (
                    "session_id",
                    "status",
                    "compressed_tokens",
                    "chunks",
                    "expected_calls",
                    "input_tokens",
                    "output_tokens",
                )
            )
        )
    totals = _estimate_totals(estimates)
    print(
        "TOTAL\t"
        + "\t".join(
            str(totals[k])
            for k in (
                "compressed_tokens",
                "chunks",
                "expected_calls",
                "input_tokens",
                "output_tokens",
            )
        )
    )


def _report_to_json(
    report: SummarizeReport,
    *,
    provider: str,
    model: str,
    selected: int,
) -> dict[str, Any]:
    payload = asdict(report)
    payload["provider"] = provider
    payload["model"] = model
    payload["selected"] = selected
    return payload


def _print_report_paragraph(report: SummarizeReport) -> None:
    print(
        "Summaries: "
        f"written {report.written}, unchanged {report.unchanged}, "
        f"skipped_small {report.skipped_small}, "
        f"skipped_no_messages {report.skipped_no_messages}, "
        f"skipped_deleted {report.skipped_deleted}, "
        f"needs_chunking {report.needs_chunking}, over_budget {report.over_budget}, "
        f"invalid {report.invalid}, failed {report.failed}, "
        f"chunk_calls {report.chunk_calls}, merge_calls {report.merge_calls}, "
        f"reused_chunks {report.reused_chunks}, truncated_sessions {report.truncated_sessions}, "
        f"redactions {report.redactions}, "
        f"input_tokens {report.input_tokens}, output_tokens {report.output_tokens}."
    )


def _collect_candidates(args: Namespace, output_dir: Path) -> tuple[Optional[int], list[str]]:
    if args.level not in ("session",):
        print(
            f"Error: --level {args.level} is not implemented yet",
            file=sys.stderr,
        )
        return 2, []

    if args.session is not None and not str(args.session).strip():
        print("Error: --session needs a non-empty session id", file=sys.stderr)
        return 2, []  # an empty value must not silently widen the run to every session

    if not v2_db_path(output_dir).exists():
        print("Error: No session store found. Run 'lore reindex' first.", file=sys.stderr)
        return 2, []  # exit 1 is reserved for "every attempted call failed / no provider"

    session_id: Optional[str] = None
    if args.session:
        try:
            session_id = resolve_session_id(output_dir, args.session)
        except SessionIdResolutionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2, []

    since: Optional[datetime] = None
    if args.since:
        since = datetime.now(timezone.utc) - parse_duration(args.since)
    elif not args.session:
        since = datetime.now(timezone.utc) - parse_duration(DEFAULT_SINCE_NO_SESSION)

    tool = None
    if args.tool:
        tool = normalize_tool_name(args.tool) or args.tool

    include_automated = bool(getattr(args, "include_automated", False))
    candidates = select_candidates(
        output_dir,
        session_id=session_id,
        project=args.project,
        since=since,
        tool=tool,
        include_automated=include_automated,
        limit=args.max_sessions,
    )
    return None, candidates


def run_summarize_command(args: Namespace) -> int:
    """Entry point for ``lore summarize``."""
    output_dir = Path(args.output_dir).expanduser()
    err, candidates = _collect_candidates(args, output_dir)
    if err is not None:
        return err

    provider_name = args.provider
    if provider_locality(provider_name) == "cloud" and not args.allow_cloud:
        print(
            f"Error: provider '{provider_name}' sends transcript text to the cloud. "
            "Pass --allow-cloud to confirm.",
            file=sys.stderr,
        )
        return 2

    model = args.model or default_model(provider_name)
    model_label = f"{provider_name}/{model}"
    max_session_tokens = args.max_session_tokens

    if args.dry_run:
        estimates = estimate_run(
            output_dir,
            candidates,
            max_sessions=args.max_sessions,
            model_label=model_label,
            max_session_tokens=max_session_tokens,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "sessions": [asdict(e) for e in estimates],
                        "totals": _estimate_totals(estimates),
                    },
                    indent=2,
                )
            )
        else:
            _print_dry_run_table(estimates)
        return 0

    from lore.llm import LLMConfig, get_provider

    try:
        config = LLMConfig.for_transcripts(provider=provider_name, model=model)
        provider = get_provider(
            provider=config.provider,
            model=config.model,
            cache_enabled=config.cache_enabled,
            options=config.options,
            num_ctx=config.num_ctx,
        )
    except Exception as exc:
        print(f"Error: Could not initialize LLM: {exc}", file=sys.stderr)
        return 1

    if not provider.is_available():
        if provider_name == "ollama":
            hint = "Start Ollama locally or choose another --provider."
        else:
            hint = "Set GEMINI_API_KEY or GOOGLE_API_KEY."
        print(f"Error: {provider_name} not available. {hint}", file=sys.stderr)
        return 1

    locality = provider_locality(provider_name)
    announcement = (
        f"Provider: {provider_name}/{model} ({locality}) - {len(candidates)} sessions selected"
    )
    if args.json:
        print(announcement, file=sys.stderr)
    else:
        print(announcement)

    report = run_summaries(
        output_dir,
        candidates,
        provider,
        model_label,
        force=args.force,
        max_sessions=args.max_sessions,
        max_input_tokens=args.max_input_tokens,
        max_session_tokens=max_session_tokens,
    )

    if args.json:
        print(
            json.dumps(
                _report_to_json(
                    report, provider=provider_name, model=model, selected=len(candidates)
                )
            )
        )
    else:
        _print_report_paragraph(report)

    if report.all_failed:
        print(
            f"Error: all {report.attempted} summarization calls failed.",
            file=sys.stderr,
        )
        return 1
    return 0
