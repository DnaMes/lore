#!/usr/bin/env python3
"""
Lore — local-first archive + shared agent memory for AI coding sessions

A local alternative to SpecStory that collects, unifies and exports chat histories
from multiple AI coding assistants without any cloud connectivity.

Supported tools:
- Claude Code
- Cursor
- VSCode Copilot
- Gemini CLI
- Codex CLI

Usage:
    lore list [--tool TOOL] [--since DURATION] [--format FORMAT]
    lore export [--all] [--tool TOOL] [--project PATH]
    lore search QUERY [--tool TOOL] [--context N]
    lore stats
    lore digest [--since 7d] [--format text|markdown]
    lore memory add|list|search|rate ...
    lore watch [--interval SECS] [--git]
    lore check
    lore sync TOOL [--session-id ID] [--project PATH]
    lore run TOOL [-- ARGS...]
    lore threads
    lore summarize [--session ID] [--since 30d] [--provider ollama] [--dry-run]
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add local package to path if running from source
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lore.core.models import AUTOMATED_ORIGINS, Tool, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.exporters.markdown import MarkdownExporter
from lore.exporters.redaction import REDACTED_EXPORT_MARKER, redact_session
from lore.extractors.base import stamp_parser_version
from lore.extractors.factory import get_all_extractors
from lore.extractors.opencode import OpenCodeExtractor
from lore.services.discovery import read_last_discovery, record_discovery
from lore.services.extraction import preserve_unobserved_index_entries
from lore.services.index import automated_hidden_line, filter_automated
from lore.services.summarize_cli import PROVIDER_CHOICES, default_model
from lore.storage.reader import load_ignored_session_ids_strict
from lore.utils.console import make_console_safe
from lore.utils.datetime import make_naive, parse_duration
from lore.utils.git import get_git_info
from lore.utils.paths import (
    get_current_project,
    lore_home,
    make_thread_id,
    restrict_file,
    secure_dir,
)
from lore.utils.rules import extract_rules
from lore.utils.tooling import normalize_tool_name

TOOL_EXECUTABLES = {
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "codex": "codex",
    "pi": "pi",
}


def _include_automated(args) -> bool:
    """``--include-automated`` (exec/cron/subagent sessions are hidden by default)."""
    return bool(getattr(args, "include_automated", False))


def _add_automated_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-automated",
        action="store_true",
        help="Also include automated (exec/cron/subagent) sessions, hidden by default",
    )


def _get_extractors_for_tool(tool: str):
    extractors = get_all_extractors()
    if not tool:
        return extractors
    tool = normalize_tool_name(tool) or tool
    return [ex for ex in extractors if ex.tool.value == tool]


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _index_entry_to_session(entry):
    created = entry.get("created")
    updated = entry.get("updated")
    try:
        created_at = datetime.fromisoformat(created) if created else datetime.fromtimestamp(0)
    except ValueError:
        created_at = datetime.fromtimestamp(0)
    try:
        updated_at = datetime.fromisoformat(updated) if updated else created_at
    except ValueError:
        updated_at = created_at

    tool_raw = entry.get("tool")
    try:
        tool = Tool(tool_raw)
    except ValueError:
        tool = Tool.CLAUDE_CODE

    return UnifiedSession(
        tool=tool,
        session_id=entry.get("id", ""),
        created_at=created_at,
        last_updated=updated_at,
        messages=[],
        project_path=entry.get("project"),
        thread_id=entry.get("thread_id"),
        title=entry.get("title"),
        origin=entry.get("origin") or "unknown",
        parent_session_id=entry.get("parent_session_id"),
        relation=entry.get("relation"),
        source_version=entry.get("source_version"),
        parser_version=_as_int(entry.get("parser_version")),
    )


def _is_low_value_index_entry(entry):
    raw_prompts = entry.get("prompts")
    raw_messages = entry.get("messages")
    if raw_prompts is None and raw_messages is None:
        return False

    prompt_count = entry.get("prompts", entry.get("messages", 0))
    message_count = entry.get("messages", 0)
    prompt_outline = (entry.get("prompt_outline") or "").strip()
    search_text = (entry.get("search_text") or "").strip()

    try:
        prompt_count = int(prompt_count or 0)
    except (TypeError, ValueError):
        prompt_count = 0

    try:
        message_count = int(message_count or 0)
    except (TypeError, ValueError):
        message_count = 0

    if prompt_count <= 1:
        return True

    if prompt_count <= 2 and message_count <= 3:
        if len(prompt_outline) < 24 and len(search_text) < 200:
            return True

    return False


def _merge_sessions_with_existing_index(all_sessions, existing_index_sessions):
    merged_by_id = {session.session_id: session for session in all_sessions}
    for entry in existing_index_sessions:
        session_id = entry.get("id")
        # Automated sessions are kept below the prompt floor on purpose.
        if _is_low_value_index_entry(entry) and entry.get("origin") not in AUTOMATED_ORIGINS:
            continue
        if session_id and session_id not in merged_by_id:
            merged_by_id[session_id] = _index_entry_to_session(entry)
    return list(merged_by_id.values())


def _load_existing_index_state(index_path: Path) -> tuple[dict[str, str], list[dict]]:
    """Read the current index.json for merge-with-extracts rebuilds (#117).

    Returns ``(export_paths, sessions)`` so callers can both keep previous
    export paths and merge session metadata. A missing, empty, or corrupt
    index yields empty values — callers then rebuild from scratch with the
    freshly extracted sessions. Previously this block was copy-pasted four
    times (cmd_export, cmd_sync twice, cmd_watch) and could drift.
    """
    export_paths: dict[str, str] = {}
    sessions: list[dict] = []
    if not index_path.exists():
        return export_paths, sessions
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            existing_index = json.load(f)
        for s in existing_index.get("sessions", []):
            if s.get("export_path"):
                export_paths[s.get("id")] = s.get("export_path")
            sessions.append(s)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return export_paths, sessions


def cmd_list(args):
    """List all sessions."""
    extractors = get_all_extractors()
    sessions = []
    tool_filter = normalize_tool_name(args.tool) if args.tool else None

    for extractor in extractors:
        if tool_filter and extractor.tool.value != tool_filter:
            continue
        if not extractor.is_available():
            continue

        for session in extractor.extract_sessions():
            # Filter by date
            if args.since:
                cutoff = datetime.now() - parse_duration(args.since)
                if make_naive(session.created_at) < cutoff:
                    continue

            # Filter by project
            if args.project and session.project_path != args.project:
                continue

            sessions.append(session)

    if getattr(args, "tag", None):
        from lore.storage import all_tags_by_session

        user_tags = all_tags_by_session(Path(args.output_dir).expanduser())
        wanted = args.tag.strip().lower()
        sessions = [
            s
            for s in sessions
            if wanted in [str(tag).lower() for tag in user_tags.get(s.session_id, [])]
        ]

    # Sort by date
    sessions.sort(key=lambda s: make_naive(s.last_updated), reverse=True)

    shown_sessions = filter_automated(sessions, _include_automated(args))
    hidden_line = automated_hidden_line(len(sessions) - len(shown_sessions))
    sessions = shown_sessions
    total_sessions = len(sessions)

    limit = args.limit if hasattr(args, "limit") and args.limit else None
    offset = args.offset if hasattr(args, "offset") and args.offset else 0

    if limit:
        sessions = sessions[offset : offset + limit]

    if args.format == "json":
        output = []
        for s in sessions:
            output.append(
                {
                    "tool": s.tool.value,
                    "session_id": s.session_id,
                    "project": s.project_path,
                    "title": s.title,
                    "created": s.created_at.isoformat(),
                    "messages": s.message_count,
                }
            )
        print(json.dumps(output, indent=2))
    else:
        # Table format
        print(f"{'Tool':<15} {'Date':<12} {'Messages':>8}  {'Project/Title'}")
        print("-" * 80)
        for s in sessions:
            date_str = s.created_at.strftime("%Y-%m-%d")
            title = s.title or s.project_path or s.session_id[:20]
            if len(title) > 40:
                title = title[:37] + "..."
            print(f"{s.tool.value:<15} {date_str:<12} {s.message_count:>8}  {title}")

    # JSON mode keeps stdout to the array alone; the summary goes to stderr.
    info = sys.stderr if args.format == "json" else sys.stdout
    if limit:
        print(
            f"\nShowing {len(sessions)} of {total_sessions} sessions (offset: {offset})",
            file=info,
        )
        if offset + limit < total_sessions:
            print(f"  → Use --offset {offset + limit} --limit {limit} to see more", file=info)
    else:
        print(f"\nTotal: {total_sessions} sessions", file=info)
    if hidden_line:
        print(f"{hidden_line} (use --include-automated to show them)", file=info)


def _tag_git_info(session, cache: dict) -> None:
    """Fill a session's missing git branch/SHA from its project directory.

    ``cache`` maps project path → ``get_git_info`` result so a project shared
    by many sessions costs one pair of ``git`` calls instead of one per session.
    """
    if session.git_branch is not None and session.git_commit is not None:
        return
    key = session.project_path or ""
    if key not in cache:
        cache[key] = get_git_info(session.project_path)
    info = cache[key]
    if session.git_branch is None:
        session.git_branch = info["branch"]
    if session.git_commit is None:
        session.git_commit = info["sha"]


def _incremental_baseline(
    index_path: Path, prior_sessions: list[dict]
) -> tuple[dict[str, int], dict[str, list[dict]], dict[str, int]]:
    """Map source path -> mtime_ns, -> entries and -> min parser version to skip.

    The index stamps ``source_mtime`` when it writes a row, which can be later
    than the moment the file was parsed. Only rows whose mtime is older than the
    previous run's ``generated_at`` (set before that scan started) are known to
    have been read completely; anything newer is parsed again. A missing or
    unreadable ``generated_at`` disables the shortcut.

    A source may hold several sessions: all their entries are kept, and the
    parser version is the minimum among them (an entry without one counts as 0).
    """
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            generated_at = datetime.fromisoformat(json.load(f)["generated_at"])
        cutoff_ns = int(generated_at.timestamp() * 1_000_000_000)
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}, {}
    known: dict[str, int] = {}
    by_path: dict[str, list[dict]] = {}
    versions: dict[str, int] = {}
    for entry in prior_sessions:
        path, mtime = entry.get("source_path"), entry.get("source_mtime")
        if path and isinstance(mtime, int) and mtime < cutoff_ns:
            known[path] = mtime
            by_path.setdefault(path, []).append(entry)
            version = entry.get("parser_version")
            version = version if isinstance(version, int) else 0
            versions[path] = min(versions.get(path, version), version)
    return known, by_path, versions


def _reusable_entries(extractor, prior_by_path: dict, ignored_ids: set) -> dict[str, dict]:
    """Prior index entries (by id) for the sources ``extractor`` skipped."""
    reusable: dict[str, dict] = {}
    for path in getattr(extractor, "skipped_sources", ()):
        for entry in prior_by_path.get(path, ()):
            session_id = entry.get("id")
            if session_id and session_id not in ignored_ids:
                reusable[session_id] = entry
    return reusable


def _redacted_export_dir_error(output_dir: Path) -> str | None:
    """Refuse redacted export into (or below) the live archive and into plain exports."""
    resolved = output_dir.resolve()
    home = lore_home().resolve()
    if resolved == home or resolved.is_relative_to(home) or home.is_relative_to(resolved):
        return (
            "Error: --redact requires --output-dir outside the default archive "
            f"({lore_home()}) and not containing it"
        )
    marker = output_dir / REDACTED_EXPORT_MARKER
    try:
        non_empty = output_dir.is_dir() and any(output_dir.iterdir())
    except OSError:
        non_empty = True
    if non_empty and not marker.exists():
        return (
            "Error: --redact needs an empty or previously redacted --output-dir "
            f"({output_dir} already holds other files)"
        )
    return None


def _print_skip_summary(extractors) -> None:
    """Explain on stderr why sessions were left out of an export or sync.

    Sums each extractor's per-run ``skip_counts`` so a first run that exports
    nothing says why (e.g. every session is below the prompt floor).
    """
    totals: dict[str, int] = {}
    min_prompts = None
    for extractor in extractors:
        counts = getattr(extractor, "skip_counts", None) or {}
        for reason, n in counts.items():
            totals[reason] = totals.get(reason, 0) + n
        if min_prompts is None and counts.get("too_few_user_prompts"):
            min_prompts = extractor._effective_thresholds()[2]
    for reason, n in sorted(totals.items()):
        if not n:
            continue
        if reason == "too_few_user_prompts":
            line = (
                f"Skipped {n} sessions with fewer than {min_prompts} user prompts "
                "(set LORE_MIN_USER_PROMPTS=1 to include them)"
            )
        elif reason == "too_little_content":
            line = f"Skipped {n} sessions with too little content"
        else:
            line = f"Skipped {n} ({reason.replace('_', ' ')})"
        print(line, file=sys.stderr)


def cmd_export(args):
    """Export sessions to Markdown."""
    output_dir = Path(args.output_dir).expanduser()
    redact_exports = bool(getattr(args, "redact", False))

    if redact_exports:
        err = _redacted_export_dir_error(output_dir)
        if err:
            print(err, file=sys.stderr)
            return 1
    elif (output_dir / REDACTED_EXPORT_MARKER).exists():
        print(
            "Error: refusing plain export into a redacted-export directory "
            f"(found {REDACTED_EXPORT_MARKER}); use a different --output-dir",
            file=sys.stderr,
        )
        return 1

    if redact_exports:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / REDACTED_EXPORT_MARKER).write_text("", encoding="utf-8")

    exporter = MarkdownExporter(output_dir)
    index_builder = IndexBuilder(output_dir)

    tool_filter = normalize_tool_name(args.tool) if args.tool else None
    git_info_cache: dict = {}

    # Export paths of earlier runs, updated in place as sessions stream past so
    # the index builder sees every fresh path without a second pass.
    index_path = output_dir / "index.json"
    merged_paths, prior_sessions = _load_existing_index_state(index_path)
    exported = 0
    total_redactions = 0

    # Incremental: a source file the last scan had already finished reading and
    # that has not changed since is not parsed again; its prior index entry is
    # reused. Only valid for an unfiltered run, because a filtered run rebuilds
    # the index from a separate full pass.
    incremental = (
        not getattr(args, "full", False)
        and not tool_filter
        and not args.project
        and not redact_exports
    )
    known_mtimes, prior_by_path, known_versions = (
        _incremental_baseline(index_path, prior_sessions) if incremental else ({}, {}, {})
    )
    ignored_ids = index_builder._load_ignored()
    reused_entries: list[dict] = []
    finished_extractors: list = []
    prior_by_id = {str(e["id"]): e for e in prior_sessions if e.get("id")}
    filtered_run = bool(tool_filter or args.project)

    def _preserve_prior(seen_ids: set[str]) -> list[dict]:
        """Prior rows not seen this run; only ignored ids are left out."""
        return preserve_unobserved_index_entries(prior_by_id, seen_ids, ignored_ids=ignored_ids)

    def export_stream():
        """Export each matching session, then yield it — one at a time.

        Sessions are deliberately never collected: keeping every
        ``UnifiedSession`` (with message bodies) alive made a full sync peak
        at ~3 GB for ~1000 sessions (lore-sync). ``IndexBuilder.build_index``
        consumes this stream and drops each session after indexing it.
        """
        nonlocal exported, total_redactions
        observed_ids: set[str] = set()
        for extractor in get_all_extractors():
            if tool_filter and extractor.tool.value != tool_filter:
                continue
            if not extractor.is_available():
                continue

            print(f"Extracting from {extractor.tool.value}...")

            extractor.known_source_mtimes = known_mtimes
            extractor.known_source_parser_versions = known_versions
            extractor.skipped_sources = set()
            skipped_by_id: dict[str, dict] = {}
            skipped_seen = -1
            # Ids a freshly parsed copy replaced; they never come back as reused.
            replaced_ids: set[str] = set()

            def reusable_view() -> dict[str, dict]:
                """Prior entries of the skipped sources, minus the replaced ids."""
                return {
                    sid: entry
                    for sid, entry in _reusable_entries(
                        extractor, prior_by_path, ignored_ids
                    ).items()
                    if sid not in replaced_ids
                }

            for session in extractor.extract_sessions():
                stamp_parser_version(extractor, session)
                # Filter by project
                if args.project and session.project_path != args.project:
                    continue

                # A streaming extractor (codex) skips sources while it yields, so
                # the skipped set grows during the loop: refresh the view whenever
                # it did, else a later skipped source would lose its entry.
                if len(extractor.skipped_sources) != skipped_seen:
                    skipped_seen = len(extractor.skipped_sources)
                    skipped_by_id = reusable_view()
                prior = skipped_by_id.get(session.session_id)
                if prior is not None:
                    # Same id also arrived from a changed copy elsewhere: the newer
                    # one wins, never both (the index id is unique).
                    if str(prior.get("updated") or "") > session.last_updated.isoformat():
                        continue
                replaced_ids.add(session.session_id)
                skipped_by_id.pop(session.session_id, None)
                observed_ids.add(session.session_id)

                _tag_git_info(session, git_info_cache)
                exported += 1

                export_target = session
                if redact_exports:
                    export_target, redactions = redact_session(session)
                    total_redactions += redactions

                # Export to markdown (skips if file is already up-to-date)
                try:
                    candidate = exporter._candidate_path(export_target)
                    was_current = (
                        not redact_exports
                        and candidate.exists()
                        and candidate.stat().st_mtime >= session.last_updated.timestamp()
                    )
                    path = exporter.export_session(export_target, force=redact_exports)
                    merged_paths[session.session_id] = path
                    if not was_current:
                        print(f"  Exported: {path}")
                except Exception as e:
                    print(f"  Error exporting {session.session_id}: {e}", file=sys.stderr)

                yield export_target

            for entry in reusable_view().values():
                sid = entry.get("id")
                if sid:
                    observed_ids.add(sid)
                reused_entries.append(entry)
            finished_extractors.append(extractor)

        # Sources deleted after the last export (retention cleanup) are never
        # skipped or re-yielded; keep their prior rows unless ignored.
        # A filtered run preserves in all_sessions_stream instead (full view).
        if not filtered_run:
            reused_entries.extend(_preserve_prior(observed_ids))

    def all_sessions_stream():
        """Yield every session from every extractor for the unfiltered index."""
        seen_ids: set[str] = set()
        for extractor in get_all_extractors():
            if not extractor.is_available():
                continue
            for session in extractor.extract_sessions():
                stamp_parser_version(extractor, session)
                _tag_git_info(session, git_info_cache)
                seen_ids.add(session.session_id)
                yield session
        # Runs after every session was seen; build_index reads reused_entries
        # only at finalize, so the rows added here are still written.
        if filtered_run:
            reused_entries.extend(_preserve_prior(seen_ids))

    # Build index. Without a --tool/--project filter the export stream already
    # covers the full set, so it feeds the index directly — re-running every
    # extractor would double the I/O, CPU and memory on a full sync (1885
    # sessions × 10 tools easily exhausts a 38 GiB box). With a filter the
    # index must still cover all tools, so export first, then stream them all.
    if not tool_filter and not args.project:
        print("Exporting sessions and building index...")
        index_builder.build_index(export_stream(), merged_paths, reused_entries=reused_entries)
    else:
        for _ in export_stream():
            pass
        print("\nBuilding index...")
        index_builder.build_index(
            all_sessions_stream(), merged_paths, reused_entries=reused_entries
        )
    print(f"Index saved to: {index_builder.index_path}")
    record_discovery(output_dir, finished_extractors)

    unchanged = f" (+{len(reused_entries)} unchanged, not re-parsed)" if reused_entries else ""
    redacted_note = f"; Redacted {total_redactions} secret-like strings" if redact_exports else ""
    print(f"\nExported {exported} sessions{unchanged} to {output_dir}{redacted_note}")
    _print_skip_summary(finished_extractors)


def _find_session_by_id(session_id: str):
    """Resolve a session id to a fully-loaded UnifiedSession from any tool.

    Matches on exact id first, then on a unique prefix so short ids work.
    """
    exact = None
    prefix_matches = []
    for extractor in get_all_extractors():
        if not extractor.is_available():
            continue
        try:
            sessions = extractor.extract_sessions()
        except Exception:
            continue
        for session in sessions:
            if session.session_id == session_id:
                exact = session
                break
            if session.session_id.startswith(session_id):
                prefix_matches.append(session)
        if exact:
            break

    if exact:
        return exact
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise ValueError(f"Ambiguous session id '{session_id}' matches {len(prefix_matches)}")
    return None


def cmd_export_html(args):
    """Export a single session as a standalone, shareable HTML file."""
    from lore.exporters.html import render_session_html
    from lore.utils.security import sanitize_filename

    try:
        session = _find_session_by_id(args.session_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if session is None:
        print(f"Error: no session found for id '{args.session_id}'", file=sys.stderr)
        return 1

    total_redactions = 0
    if getattr(args, "redact", False):
        session, total_redactions = redact_session(session)
        if total_redactions:
            print(
                f"Redacted {total_redactions} secret-like strings",
                file=sys.stderr,
            )

    # The default filename is derived from the session id — sanitise it so a
    # malformed extractor id (e.g. containing '/' or '..') cannot escape cwd.
    try:
        default_name = sanitize_filename(f"{session.session_id}.html")
    except ValueError:
        default_name = "session-export.html"

    output_path = Path(args.output).expanduser() if args.output else Path.cwd() / default_name
    if output_path.is_dir():
        output_path = output_path / default_name

    # Do NOT create parent directories implicitly — a typo'd --output should
    # fail loudly, not scatter directories across the filesystem.
    if not output_path.parent.exists():
        print(
            f"Error: output directory does not exist: {output_path.parent}",
            file=sys.stderr,
        )
        return 1

    html_doc = render_session_html(session)
    output_path.write_text(html_doc, encoding="utf-8")
    # The export contains full transcript content — restrict it like the
    # other transcript-bearing files (issue #41 threat model).
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass

    print(f"Exported session {session.session_id} ({session.message_count} messages)")
    print(f"  → {output_path}")
    return 0


# Export files are named "<date>_<title>_<id>.md" inside "projects/", so none of
# these prefixes can belong to an export. Store and control files never can.
_PROTECTED_NAME_PREFIXES = (
    "index",
    "ignored",
    "deleted_sessions",
    "title_cache",
    "memory",
    "memories",
    "opencode_state",
    "rules",
)

# Deletable-export heuristic, mirroring MarkdownExporter._candidate_path
# (lore/exporters/markdown.py): the exporter writes
# <root>/projects/<project dir>/<tool dir>/<YYYY-MM-DD>_<title>_<id>.md.
# Both the basename pattern and the directory layout are checked, so a stray
# .md anywhere else in the archive (e.g. backups/notes.md) is never deleted.
_EXPORT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_.+_.+\.md$")
_EXPORT_LAYOUT_DIR = "projects"
_EXPORT_LAYOUT_DEPTH = 4  # projects/<project dir>/<tool dir>/<filename>


def _confined_export_path(export_path: str | Path, export_root: Path) -> Path | None:
    """Return the resolved export file prune may delete, or ``None`` to refuse.

    Deletable means a regular ``.md`` file under ``export_root`` whose basename
    matches the exporter's naming scheme and whose path follows the exporter's
    ``projects/<project>/<tool>/`` layout (``export_path`` always comes from an
    index entry, which is the source of the path). Anything else — including
    store and control file names — is refused. A symlink at the entry is
    refused outright; parent directories are resolved, so a link chain that
    leaves the root is refused as well.
    """
    root = export_root.expanduser().resolve()
    raw = Path(export_path).expanduser()
    try:
        if raw.is_symlink():
            return None
        resolved = raw.resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(root) or resolved.parent == root:
        return None
    if resolved.suffix != ".md" or resolved.name.endswith(("-wal", "-shm")):
        return None
    if not _EXPORT_FILENAME_RE.match(resolved.name):
        return None
    if resolved.name.startswith(_PROTECTED_NAME_PREFIXES):
        return None
    relative = resolved.relative_to(root)
    if len(relative.parts) != _EXPORT_LAYOUT_DEPTH or relative.parts[0] != _EXPORT_LAYOUT_DIR:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def cmd_prune(args):
    """Prune sessions from the export/index based on prompt count."""
    output_dir = Path(args.output_dir).expanduser()
    index_path = output_dir / "index.json"
    if not index_path.exists():
        print(f"No index found at {index_path}. Run `lore export --all` first.")
        return

    # Read the existing ignore set before anything is deleted or rewritten:
    # an unreadable/invalid ignored.json must abort (rewriting it would drop
    # every previously pruned id we could not parse). A missing file is fine.
    ignore_path = output_dir / "ignored.json"
    try:
        ignored = load_ignored_session_ids_strict(output_dir)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Fix or move ignored.json before pruning; nothing was deleted.", file=sys.stderr)
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Automated sessions sit below the prompt floor by design: never pruned unless asked.
    candidates = [
        s
        for s in filter_automated(index.get("sessions", []), _include_automated(args))
        if s.get("prompts", s.get("messages", 0)) <= args.max_prompts
    ]
    if not candidates:
        print(f"No sessions found with {args.max_prompts} prompts or fewer.")
        return

    print(f"Found {len(candidates)} sessions with {args.max_prompts} prompts or fewer.")
    if args.dry_run:
        for s in candidates[:20]:
            prompt_count = s.get("prompts", s.get("messages", 0))
            print(f"  {s.get('id')} • {s.get('tool')} • {prompt_count} prompts")
        if len(candidates) > 20:
            print(f"  ...and {len(candidates) - 20} more")
        return

    removed = []
    skipped_paths = 0
    for s in candidates:
        session_id = s.get("id")
        ignored.add(session_id)
        removed.append(session_id)
        export_path = s.get("export_path")
        if not export_path or not os.path.lexists(Path(export_path).expanduser()):
            continue
        confined = _confined_export_path(export_path, output_dir)
        if confined is None:
            print(
                f"Warning: refusing to delete export outside archive root: {export_path}",
                file=sys.stderr,
            )
            skipped_paths += 1
            continue
        try:
            confined.unlink()
        except OSError:
            pass

    # Union of previously ignored ids and the freshly pruned ones, written as a
    # plain sorted list — the canonical form cmd_prune has always written and
    # every reader (load_ignored_session_ids) accepts.
    ignore_path.write_text(json.dumps(sorted(ignored), indent=2), encoding="utf-8")

    extractors = get_all_extractors()
    sessions = []
    for extractor in extractors:
        if not extractor.is_available():
            continue
        sessions.extend(extractor.extract_sessions())

    export_paths = {}
    for s in index.get("sessions", []):
        if s.get("export_path") and s.get("id") not in ignored:
            export_paths[s.get("id")] = s.get("export_path")

    # Keep entries whose source is not extracted now (other offline sessions);
    # only the ids in ``ignored`` leave the index.
    prior_by_id = {str(s["id"]): s for s in index.get("sessions", []) if s.get("id")}
    kept = preserve_unobserved_index_entries(
        prior_by_id,
        {s.session_id for s in sessions},
        ignored_ids=ignored,
    )
    IndexBuilder(output_dir).build_index(sessions, export_paths, reused_entries=kept)
    print(f"Pruned {len(removed)} sessions and rebuilt index.")
    if skipped_paths:
        print(
            f"Skipped {skipped_paths} export path(s) outside the archive root.",
            file=sys.stderr,
        )


def cmd_reindex(args):
    """Rebuild the index from all tool sources.

    Fixes the bug where build_index only indexes the current sync run.
    Re-extracts from all available tools and rebuilds the full index.
    """
    output_dir = Path(args.output_dir).expanduser()
    all_tools = [
        "opencode",
        "claude-code",
        "vscode",
        "codex",
        "cursor",
        "gemini-cli",
        "pi",
    ]

    all_sessions = []
    export_paths = {}
    projects_dir = output_dir / "projects"

    # Build export_path map from .md headers (fast, no extractor needed)
    print("Scanning exported .md files...")
    import re as _re

    for md_file in projects_dir.rglob("*.md"):
        try:
            with open(md_file, "r", encoding="utf-8", errors="replace") as f:
                header = f.read(300)
            m = _re.search(r"session_id:\s*(\S+)", header)
            if m:
                export_paths[m.group(1)] = md_file
        except OSError:
            pass
    print(f"  Found {len(export_paths)} exported sessions on disk")

    # Extract sessions from all tools (uses cache, fast for already-seen)
    for tool_name in all_tools:
        if tool_name == "opencode":
            extractors = [OpenCodeExtractor(force_full=True)]
        else:
            extractors = _get_extractors_for_tool(tool_name)
        for extractor in extractors:
            if not extractor.is_available():
                continue
            try:
                count_before = len(all_sessions)
                for session in extractor.extract_sessions():
                    all_sessions.append(session)
                added = len(all_sessions) - count_before
                if added:
                    print(f"  {tool_name}: {added} sessions")
            except Exception as e:
                print(f"  Warning: {tool_name}: {e}", file=sys.stderr)

    print(f"\nTotal: {len(all_sessions)} sessions loaded")
    print("Rebuilding index...")
    IndexBuilder(output_dir).build_index(all_sessions, export_paths)
    print(f"Done. Index: {output_dir / 'index.json'}")
    print(f"SQLite: {output_dir / 'index.sqlite'}")


def _cloud_optin_error(args) -> int | None:
    """Exit code 2 when a cloud provider is chosen without opt-in, else None.

    Every non-local provider (gemini, openrouter, jev, off-host ollama) needs
    ``--allow-cloud`` or ``LORE_ALLOW_CLOUD=1``.
    """
    from lore.services.summarize_cli import require_cloud_opt_in

    provider = getattr(args, "provider", None)
    if not provider:
        return None
    return require_cloud_opt_in(provider, bool(getattr(args, "allow_cloud", False)))


def cmd_analyze(args):
    """Generate AI-powered statistics and insights."""
    from lore.llm import LLMConfig, get_provider
    from lore.llm.tasks import StatsGenerator

    output_dir = Path(args.output_dir).expanduser()
    index_file = output_dir / "index.json"

    if not index_file.exists():
        print("Error: No index found. Run 'lore reindex' first.", file=sys.stderr)
        return 1

    with open(index_file, "r") as f:
        index = json.load(f)

    sessions = [
        _index_entry_to_session(s)
        for s in filter_automated(index.get("sessions", []), _include_automated(args))
    ]

    print(f"Analyzing {len(sessions)} sessions...")

    provider = None
    if not args.no_llm:
        if (rc := _cloud_optin_error(args)) is not None:
            return rc
        try:
            config = LLMConfig(
                provider=args.provider,
                model=args.model or default_model(args.provider),
            )
            provider = get_provider(
                provider=args.provider,
                model=config.model,
                allow_cloud=bool(getattr(args, "allow_cloud", False)),
            )
            if provider.is_available():
                print(f"Using {args.provider} ({config.model}) for insights...")
            else:
                print(f"Warning: {args.provider} not available, using basic stats only")
                provider = None
        except Exception as e:
            print(f"Warning: Could not initialize LLM: {e}")
            provider = None

    generator = StatsGenerator(provider, output_dir / "stats")
    stats = generator.generate_session_stats(sessions, use_llm=provider is not None)

    output_file = Path(args.output) if args.output else generator.output_dir / "stats.json"
    generator.save_stats(stats, output_file.name)

    print(f"\nStatistics saved to: {output_file}")
    print("\nSummary:")
    print(f"  Total sessions: {stats['total_sessions']}")
    print(f"  Total messages: {stats['total_messages']}")
    print(f"  Last 7 days: {stats['sessions_last_7_days']}")
    print(f"  Last 30 days: {stats['sessions_last_30_days']}")

    if "insights" in stats:
        print("\nAI Insights:")
        for key, value in stats["insights"].items():
            print(f"  {key}: {value}")


def cmd_summarize(args):
    """Generate AI session summaries (local-first; cloud requires --allow-cloud)."""
    from lore.services.summarize_cli import run_summarize_command

    return run_summarize_command(args)


def cmd_improve(args):
    """Run the improve pipeline (embeddings, extraction, memory candidates)."""
    import json as json_mod

    from lore.services.improve import run_improve
    from lore.services.summarize_cli import require_cloud_opt_in

    output_dir = Path(args.output_dir).expanduser()
    stages = args.stage if getattr(args, "stage", None) else None
    provider = getattr(args, "provider", None)
    chosen = provider or os.environ.get("LORE_LLM_PROVIDER", "").strip()
    if chosen and not getattr(args, "dry_run", False):
        # extract_knowledge sends conversation text to the provider: cloud
        # providers need --allow-cloud or LORE_ALLOW_CLOUD=1 like the other
        # cloud commands.
        allow_cloud = bool(getattr(args, "allow_cloud", False))
        if (rc := require_cloud_opt_in(chosen, allow_cloud)) is not None:
            return rc
        print(
            f"Conversation text may be sent to provider '{chosen}' for extract_knowledge.",
            file=sys.stderr,
        )

    results = run_improve(
        output_dir,
        provider_name=provider,
        model=getattr(args, "model", None),
        limit=args.limit,
        stages=stages,
        dry_run=bool(getattr(args, "dry_run", False)),
        include_automated=_include_automated(args),
        allow_cloud=bool(getattr(args, "allow_cloud", False)),
    )

    if args.json:
        print(json_mod.dumps([r.to_dict() for r in results], indent=2))
    else:
        print("stage | status | reason | counts | ms")
        for row in results:
            counts = ",".join(f"{k}={v}" for k, v in sorted(row.counts.items()))
            print(f"{row.stage} | {row.status} | {row.reason} | {counts} | {row.duration_ms}")

    return 1 if any(r.status == "errored" for r in results) else 0


def cmd_knowledge(args):
    """Extract knowledge from sessions using LLM."""
    if not getattr(args, "provider", None):
        print(
            "Error: lore knowledge requires --provider (e.g. ollama). "
            "Cloud providers also need --allow-cloud or LORE_ALLOW_CLOUD=1.",
            file=sys.stderr,
        )
        return 2
    if (rc := _cloud_optin_error(args)) is not None:
        return rc
    from lore.llm import LLMConfig, get_provider
    from lore.llm.tasks import KnowledgeExtractor
    from lore.llm.tasks.knowledge import KnowledgeEntry
    from lore.services.index import load_deleted_session_ids
    from lore.services.knowledge import run_extraction
    from lore.storage import list_session_knowledge

    output_dir = Path(args.output_dir).expanduser()
    index_file = output_dir / "index.json"

    if not index_file.exists():
        print("Error: No index found. Run 'lore reindex' first.", file=sys.stderr)
        return 1

    with open(index_file, "r") as f:
        index = json.load(f)

    # Deleted sessions must not be sent to a provider, and a stored extraction of one
    # must not reappear in the knowledge base.
    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    # Automated sessions are neither sent to a provider nor shown in the knowledge base
    # unless --include-automated is given (the extraction of one stays stored).
    include_automated = _include_automated(args)
    hidden_ids = {
        str(e.get("id"))
        for e in index.get("sessions", [])
        if not include_automated and e.get("origin") in AUTOMATED_ORIGINS
    }
    from lore.services.knowledge import sessions_for_extraction

    sessions = sessions_for_extraction(
        output_dir,
        limit=args.limit,
        include_automated=include_automated,
        tool=args.tool,
    )

    in_scope: set[str] | None = None
    if args.tool:
        # Every session of the tool, not just the --limit newest: stored rows of older
        # sessions of this tool stay in the report.
        in_scope = {
            s.session_id
            for s in sessions_for_extraction(
                output_dir,
                limit=10**9,
                include_automated=include_automated,
                tool=args.tool,
            )
        }

    print(f"Extracting knowledge from {len(sessions)} sessions...")

    try:
        config = LLMConfig.for_transcripts(
            provider=args.provider,
            model=args.model or default_model(args.provider),
        )
        provider = get_provider(
            provider=config.provider,
            model=config.model,
            cache_enabled=config.cache_enabled,
            options=config.options,
            num_ctx=config.num_ctx,
            allow_cloud=bool(getattr(args, "allow_cloud", False)),
        )

        if not provider.is_available():
            print(f"Error: {args.provider} not available. Set GEMINI_API_KEY or GOOGLE_API_KEY.")
            return 1

    except Exception as e:
        print(f"Error: Could not initialize LLM: {e}")
        return 1

    extractor = KnowledgeExtractor(provider, output_dir / "knowledge")

    # Conversation text leaves the machine for cloud providers: say so first.
    print(
        f"Sending conversation text of up to {len(sessions)} sessions to "
        f"provider '{args.provider}', model '{config.model}'."
    )

    try:
        # The provider is part of the cache key: the same model name on another
        # provider is a different extraction.
        report = run_extraction(output_dir, sessions, extractor, f"{args.provider}/{config.model}")
        stored = [
            row
            for row in list_session_knowledge(output_dir)
            if row["session_id"] not in deleted
            and row["session_id"] not in hidden_ids
            and (in_scope is None or row["session_id"] in in_scope)
        ]
    except sqlite3.Error as e:
        print(f"Error: Could not use the session store: {e}", file=sys.stderr)
        return 1

    print(
        f"Extracted {report.extracted}, unchanged {report.unchanged}, "
        f"skipped (no synced messages) {report.skipped_no_messages}, failed {report.failed}"
    )
    if report.redactions > 0:
        print(
            f"Redacted {report.redactions} secret-like strings before sending",
            file=sys.stderr,
        )

    if report.failed and not report.extracted:
        # Every attempted call failed (outage, quota, bad key): scripts must see it,
        # and the message says which kind of failure it was (#199).
        print(
            f"Error: all {report.failed} extraction calls failed ({report.failure_breakdown()}).",
            file=sys.stderr,
        )
        return 1

    output_file = Path(args.output) if args.output else extractor.output_dir / "knowledge_base.json"
    if not stored:
        print("No knowledge entries stored yet.")
        # An earlier file would keep the text of sessions deleted since the last run.
        if (extractor.output_dir / output_file.name).exists():
            extractor.build_knowledge_base([], output_file.name)
        return 0

    entries = [KnowledgeEntry.from_dict(row["payload"]) for row in stored]
    extractor.build_knowledge_base(entries, output_file.name)

    if report.extracted == 0 and report.failed == 0:
        print("Nothing new to extract.")
    print(f"\nKnowledge base saved to: {output_file}")
    print(f"Total entries: {len(entries)}")
    return 0


def cmd_format(args):
    """Format sessions with AI-generated summaries and tags."""
    if (rc := _cloud_optin_error(args)) is not None:
        return rc
    from lore.llm import LLMConfig, get_provider
    from lore.llm.tasks import SessionFormatter

    output_dir = Path(args.output_dir).expanduser()
    index_file = output_dir / "index.json"

    if not index_file.exists():
        print("Error: No index found. Run 'lore reindex' first.", file=sys.stderr)
        return 1

    with open(index_file, "r") as f:
        index = json.load(f)

    entries = index.get("sessions", [])
    if not args.session_id:  # an explicit id is honoured, hidden or not
        entries = filter_automated(entries, _include_automated(args))
    sessions = [_index_entry_to_session(s) for s in entries]

    if args.session_id:
        sessions = [s for s in sessions if s.session_id == args.session_id]
    elif args.tool:
        tool = normalize_tool_name(args.tool) or args.tool
        sessions = [s for s in sessions if s.tool.value == tool]

    sessions = sorted(sessions, key=lambda s: s.created_at, reverse=True)[: args.limit]

    if not sessions:
        print("No sessions found matching criteria.")
        return 1

    print(f"Formatting {len(sessions)} sessions...")

    try:
        config = LLMConfig.for_transcripts(
            provider=args.provider,
            model=args.model or default_model(args.provider),
        )
        provider = get_provider(
            provider=config.provider,
            model=config.model,
            cache_enabled=config.cache_enabled,
            options=config.options,
            num_ctx=config.num_ctx,
            allow_cloud=bool(getattr(args, "allow_cloud", False)),
        )

        if not provider.is_available():
            print(f"Error: {args.provider} not available. Set GEMINI_API_KEY or GOOGLE_API_KEY.")
            return 1

    except Exception as e:
        print(f"Error: Could not initialize LLM: {e}")
        return 1

    output_path = Path(args.output_dir) if args.output_dir else output_dir / "formatted"
    formatter = SessionFormatter(provider, output_path)

    for i, session in enumerate(sessions, 1):
        print(f"  [{i}/{len(sessions)}] Formatting {session.session_id[:8]}...")
        try:
            formatted = formatter.format_session(session)
            path = formatter.save_formatted_session(formatted)
            print(f"    Saved: {path}")
        except Exception as e:
            print(f"    Error: {e}")

    print(f"\nFormatted sessions saved to: {output_path}")


def cmd_sync(args):
    """Sync sessions for a single tool into the export directory."""
    output_dir = Path(args.output_dir).expanduser()
    exporter = MarkdownExporter(output_dir)
    index_builder = IndexBuilder(output_dir)

    extractors = _get_extractors_for_tool(args.tool)
    sessions = []
    export_paths = {}

    for extractor in extractors:
        if not extractor.is_available():
            continue

        print(f"Syncing from {extractor.tool.value}...")
        for session in extractor.extract_sessions():
            if args.project and session.project_path != args.project:
                continue
            if args.session_id and session.session_id != args.session_id:
                continue

            sessions.append(session)
            try:
                path = exporter.export_session(session)
                export_paths[session.session_id] = path
                print(f"  Exported: {path}")
            except Exception as e:
                print(f"  Error exporting {session.session_id}: {e}", file=sys.stderr)

    print("\nBuilding index...")
    all_sessions = []
    built_extractors = []
    for extractor in get_all_extractors():
        if not extractor.is_available():
            continue
        built_extractors.append(extractor)
        all_sessions.extend(extractor.extract_sessions())

    existing_paths, existing_index_sessions = _load_existing_index_state(output_dir / "index.json")

    merged_sessions = _merge_sessions_with_existing_index(all_sessions, existing_index_sessions)

    merged_paths = {**existing_paths, **export_paths}

    print("Ensuring export files for indexed sessions...")
    created_exports = 0
    for session in merged_sessions:
        current = merged_paths.get(session.session_id)
        if current and Path(str(current)).exists():
            continue
        if session.message_count == 0:
            continue
        try:
            path = exporter.export_session(session)
            merged_paths[session.session_id] = path
            created_exports += 1
        except Exception as e:
            print(
                f"  Warning: failed to export {session.session_id}: {e}",
                file=sys.stderr,
            )

    if created_exports:
        print(f"  Created {created_exports} missing exports")

    index_builder.build_index(merged_sessions, merged_paths)
    print(f"Index saved to: {index_builder.index_path}")
    print(f"\nSynced {len(sessions)} sessions to {output_dir}")
    _print_skip_summary(built_extractors)


def _open_private_append(path: Path):
    """Open ``path`` for binary append: created 0600, and an existing file is tightened."""
    handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    try:
        restrict_file(path)
    except BaseException:
        handle.close()
        raise
    return handle


def cmd_run(args):
    """Run a tool and sync history after it exits."""
    tool = normalize_tool_name(args.tool) or args.tool
    executable = TOOL_EXECUTABLES.get(tool)
    if not executable:
        print(f"Unknown or unsupported tool for run: {tool}")
        print(f"Supported: {', '.join(sorted(TOOL_EXECUTABLES.keys()))}")
        return

    tool_args = args.tool_args or []
    if tool_args[:1] == ["--"]:
        tool_args = tool_args[1:]
    cmd = [executable] + tool_args
    print(f"Running: {' '.join(cmd)}")

    output_dir = Path(args.output_dir).expanduser()
    # Raw transcripts: private directory, and 0600 logs from the first byte.
    runs_dir = secure_dir(output_dir / "runs")
    start_ts = datetime.now()
    log_name = f"{start_ts.strftime('%Y%m%d_%H%M%S')}_{tool}.log"
    log_path = runs_dir / log_name

    project_path = get_current_project()
    thread_id = make_thread_id(project_path=project_path)

    def run_with_pty() -> int:
        # Unix only: importing pty at module level made the whole CLI unusable on Windows.
        import pty

        log_file = _open_private_append(log_path)

        def read(fd):
            data = os.read(fd, 1024)
            if data:
                log_file.write(data)
                log_file.flush()
            return data

        try:
            return pty.spawn(cmd, read)
        finally:
            log_file.close()

    exit_status = 0
    try:
        if args.no_pty:
            subprocess.run(cmd, check=False, shell=False, timeout=3600)
        else:
            try:
                exit_status = run_with_pty()
            except ImportError:
                print(
                    "Pseudo-terminals are unavailable here; running without one.", file=sys.stderr
                )
                subprocess.run(cmd, check=False, shell=False, timeout=3600)
    except FileNotFoundError:
        print(f"Executable not found: {executable}")
        return
    except KeyboardInterrupt:
        pass
    except subprocess.TimeoutExpired:
        print("Command timed out after 1 hour", file=sys.stderr)
    except Exception as e:
        print(f"Error running command: {e}", file=sys.stderr)
        exit_status = 1

    end_ts = datetime.now()
    meta_path = log_path.with_suffix(".json")
    meta = {
        "tool": tool,
        "command": cmd,
        "cwd": str(Path.cwd()),
        "thread_id": thread_id,
        "project_path": project_path,
        "start_time": start_ts.isoformat(),
        "end_time": end_ts.isoformat(),
        "exit_status": exit_status,
        "log_path": str(log_path),
    }
    meta_fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(meta_fd, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    restrict_file(meta_path)

    sync_args = argparse.Namespace(
        tool=tool,
        session_id=None,
        project=None,
        output_dir=args.output_dir,
    )
    cmd_sync(sync_args)


def cmd_search(args):
    """Search across sessions."""
    output_dir = Path(args.output_dir).expanduser()
    # Route through the shared search service so the CLI, web UI and MCP all hit
    # the same backend: v2 FTS + hybrid semantic fusion when available, with the
    # legacy SearchEngine as the fallback. Previously this called SearchEngine
    # directly, bypassing v2 and hybrid search entirely (#87).
    from lore.services.index import load_deleted_session_ids, search_index
    from lore.storage import v2_db_path

    index_path = output_dir / "index.json"
    if not index_path.exists() and not v2_db_path(output_dir).exists():
        print("No index found. Run 'lore export --all' first.")
        return
    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    page = getattr(args, "page", 1) or 1
    if page < 1:
        print("Error: --page must be 1 or higher", file=sys.stderr)
        return 1
    per_page = 20
    try:
        results = search_index(
            index_path,
            args.query,
            deleted,
            tool=args.tool,
            project=args.project,
            include_automated=_include_automated(args),
            tag=getattr(args, "tag", None) or None,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        from lore.services.index import count_index_hits

        total = count_index_hits(
            index_path,
            args.query,
            deleted,
            tool=args.tool,
            project=args.project,
            include_automated=_include_automated(args),
            tag=getattr(args, "tag", None) or None,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No results found.")
        return

    noun = "result" if (total or len(results)) == 1 else "results"
    found = total if total is not None else len(results)
    page_note = "" if page == 1 else f" (page {page}, 20 per page)"
    print(f"Found {found} {noun} for '{args.query}'{page_note}:\n")

    for r in results:
        session = r["session"]
        score = r["score"]
        print(f"[{session['tool']}] {session.get('title', session['id'][:20])}")
        print(
            f"  Score: {float(score):.3f} | Date: {session['created'][:10]} | Messages: {session['messages']}"
        )
        evidence = r.get("evidence") or None
        if evidence:
            marker = "…" if evidence.get("truncated") else ""
            print(
                f"  Evidence: #{evidence['seq']} ({evidence['role']}) "
                f"/session/{session['id']}/m/{evidence['seq']} {evidence['excerpt']}{marker}"
            )
        if session.get("export_path"):
            print(f"  File: {session['export_path']}")
        print()


def cmd_stats(args):
    """Show statistics."""
    output_dir = Path(args.output_dir).expanduser()
    index_path = output_dir / "index.json"

    if not index_path.exists():
        print("No index found. Run 'lore export --all' first.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    from lore.services.index import filter_automated_payload

    index = filter_automated_payload(index, _include_automated(args))
    stats = index.get("stats", {})

    print("AI History Statistics")
    print("=" * 40)
    print(f"Total Sessions: {stats.get('total_sessions', 0)}")
    hidden_line = automated_hidden_line(int(index.get("automated_hidden") or 0))
    if hidden_line:
        print(f"({hidden_line}; use --include-automated to show them)")
    print(f"Total Messages: {stats.get('total_messages', 0)}")
    print(f"Generated: {index.get('generated_at', 'Unknown')}")
    print()

    print("Sessions by Tool:")
    for tool, count in stats.get("by_tool", {}).items():
        print(f"  {tool}: {count}")
    print()

    print("Top Projects:")
    projects = stats.get("by_project", {})
    sorted_projects = sorted(projects.items(), key=lambda x: x[1], reverse=True)
    for project, count in sorted_projects[:10]:
        print(f"  {project}: {count}")


def cmd_digest(args):
    """Print a periodic activity digest (sessions by day, tool, project)."""
    output_dir = Path(args.output_dir).expanduser()
    index_path = output_dir / "index.json"

    if not index_path.exists():
        print("No index found. Run 'lore export --all' first.")
        sys.exit(1)

    try:
        cutoff = datetime.now() - parse_duration(args.since)
    except ValueError as exc:
        print(f"Invalid --since value: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    from lore.digest import build_digest, format_digest

    digest = build_digest(
        filter_automated(index.get("sessions", []), _include_automated(args)), since=cutoff
    )
    fmt = "markdown" if args.format == "markdown" else "text"
    print(format_digest(digest, fmt=fmt))


def cmd_memory(args):
    """Manage the shared cross-tool memory store (issue #44 / #33)."""
    output_dir = Path(args.output_dir).expanduser()
    action = args.memory_action

    if action == "context":
        from lore.services.memory_context import build_memory_context

        project = getattr(args, "project", None)
        scope = Path(project).expanduser().resolve() if project else Path.cwd().resolve()
        text = build_memory_context(
            output_dir,
            scope_project=scope,
            scope_tool=getattr(args, "tool", None),
            limit=args.limit,
            budget=args.budget,
            output_format=args.format,
        )
        if text:
            print(text)
        return

    from lore.storage import (
        MEMORY_KINDS,
        add_memory,
        feedback_weight,
        list_memory,
        recall_memory,
        record_feedback,
    )

    if action == "dedupe":
        from lore.services.memory_dedupe import dedupe_memories, restore_dedupe_undo

        if args.restore:
            undo_path = Path(args.restore).expanduser()
            if not undo_path.is_file():
                print(f"Error: undo log not found at {undo_path}", file=sys.stderr)
                sys.exit(1)
            try:
                restored, skipped, missing = restore_dedupe_undo(output_dir, undo_path)
            except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
                print(f"Could not restore dedupe undo: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Restored: {restored}; already not rejected: {skipped}; missing: {missing}")
            return

        try:
            groups, undo_path = dedupe_memories(output_dir, apply=args.apply)
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"Could not deduplicate memory: {exc}", file=sys.stderr)
            sys.exit(1)
        mode = "apply" if args.apply else "dry run, nothing written (pass --apply to reject)"
        print(f"Memory dedupe ({mode})")
        for group in groups:
            rejected = ", ".join(f"#{memory_id}" for memory_id in group.rejected_ids)
            print(f"{group.title}: keep #{group.kept_id}; reject {rejected}")
        count = sum(len(group.rejected_ids) for group in groups)
        print(
            f"{len(groups)} duplicate groups. {'Rejected' if args.apply else 'Would reject'}: {count}"
        )
        if undo_path is not None:
            print(f"Undo log: {undo_path}")
        return

    if action == "import-claude":
        from lore.services.claude_memory_import import import_claude_memory

        root = Path(args.root).expanduser() if args.root else Path.home() / ".claude" / "projects"
        if not root.is_dir():
            print(f"Error: no Claude projects directory at {root}", file=sys.stderr)
            sys.exit(1)
        try:
            report = import_claude_memory(output_dir, root, apply=args.apply)
        except sqlite3.OperationalError:
            print("Could not write memory: database is busy, try again", file=sys.stderr)
            sys.exit(1)
        mode = "apply" if args.apply else "dry run, nothing written (pass --apply to write)"
        print(f"Claude memory import ({mode}) from {report.root}")
        print(f"Scanned {report.scanned} files.")
        types = ", ".join(f"{k} {v}" for k, v in sorted(report.by_type.items())) or "none"
        print(f"Importable by type: {types}")
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(report.skipped.items())) or "none"
        print(f"Skipped: {skipped}")
        verb = "Wrote" if args.apply else "Would write"
        print(
            f"{verb}: {report.new} new, {report.updated} updated (supersedes the previous "
            f"import), {report.already} already imported, {report.duplicates} duplicates, "
            f"{report.failed} failed."
        )
        print(f"Redactions {'applied' if args.apply else 'to apply'}: {report.redactions}")
        for note in report.notes:
            print(note)
        return

    if action == "show":
        from lore.storage import get_memory

        entry = get_memory(output_dir, args.memory_id)
        if entry is None:
            print(f"Error: memory #{args.memory_id} does not exist.", file=sys.stderr)
            sys.exit(1)
        print(f"Status: {entry.status}")
        print(f"Kind: {entry.kind}")
        if entry.scope_project:
            print(f"Scope project: {entry.scope_project}")
        if entry.scope_tool:
            print(f"Scope tool: {entry.scope_tool}")
        if entry.superseded_by is not None:
            print(f"superseded by #{entry.superseded_by}")
        from lore.storage.memory import list_superseded_by

        for other_id in list_superseded_by(output_dir, entry.id):
            print(f"supersedes #{other_id}")
        print(entry.title)
        print()
        print(entry.body)
        if entry.tags:
            print()
            print(f"Tags: {', '.join(entry.tags)}")
        if entry.sources:
            print()
            print("Provenance:")
            for src in entry.sources:
                session_id = src.get("session_id") or ""
                note = src.get("note")
                line = f"  session {session_id}"
                if note:
                    line += f" — {note}"
                print(line)
        return

    if action == "review":
        from lore.storage import list_memory, set_memory_status

        target = getattr(args, "target", None)
        review_ids = getattr(args, "ids", None) or []
        if target is None:
            candidates = list_memory(
                output_dir, kind=getattr(args, "kind", None), limit=200, status="candidate"
            )
            if getattr(args, "json", False):
                payload = {
                    "count": len(candidates),
                    "candidates": [
                        {
                            "id": entry.id,
                            "kind": entry.kind,
                            "title": entry.title,
                            "body": entry.body,
                            "scope_project": entry.scope_project,
                            "scope_tool": entry.scope_tool,
                            "created": entry.created,
                        }
                        for entry in candidates
                    ],
                }
                print(json.dumps(payload, indent=2))
                return
            if not candidates:
                print("No memory candidates waiting for review.")
                return
            print(f"{len(candidates)} candidate(s) waiting for review:")
            for entry in candidates:
                scope = entry.scope_project or "global"
                print(f"  #{entry.id} [{entry.kind}] {entry.title} ({scope})")
            print("\nAccept or reject with: lore memory review accept|reject <id> [...]")
            return

        if not review_ids:
            print("Error: give at least one memory id to review", file=sys.stderr)
            sys.exit(1)
        new_status = "confirmed" if target == "accept" else "rejected"
        changed, skipped = [], []
        for raw_id in review_ids:
            try:
                memory_id = int(raw_id)
            except (TypeError, ValueError):
                print(f"Error: invalid memory id {raw_id!r}", file=sys.stderr)
                sys.exit(1)
            try:
                if set_memory_status(output_dir, memory_id, new_status, from_status="candidate"):
                    changed.append(memory_id)
                else:
                    skipped.append(memory_id)
            except ValueError:
                # Not in the expected source state (already confirmed, rejected or
                # unknown): report it instead of aborting the whole batch.
                skipped.append(memory_id)
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "action": target,
                        "status": new_status,
                        "changed": changed,
                        "unchanged": skipped,
                    },
                    indent=2,
                )
            )
            return
        noun = "candidate" if len(changed) == 1 else "candidates"
        if changed:
            print(f"{target.title()}ed {len(changed)} {noun} ({new_status}): {changed}")
        if skipped:
            print(f"No change for ids: {skipped} (not a candidate)")
        return

    if action == "repair-index":
        from lore.storage import memory_index_health, repair_memory_index

        health = repair_memory_index(output_dir, apply=bool(getattr(args, "apply", False)))
        if getattr(args, "json", False):
            payload = {
                "total_memories": health.total_memories,
                "indexed": health.indexed,
                "missing": health.missing,
                "orphans": health.orphans,
                "healthy": health.healthy,
                "applied": bool(getattr(args, "apply", False)),
            }
            print(json.dumps(payload, indent=2))
            return
        before = memory_index_health(output_dir) if getattr(args, "apply", False) else health
        if health.healthy:
            print(
                f"Memory search index is healthy ({health.total_memories} memories, "
                f"{health.indexed} indexed)."
            )
            return
        print(f"Memories: {before.total_memories}, indexed: {before.indexed}")
        if before.missing:
            print(f"Missing search rows for {len(before.missing)} memories: {before.missing[:20]}")
        if before.orphans:
            print(f"Search rows without a memory: {len(before.orphans)} ({before.orphans[:10]})")
        if getattr(args, "apply", False):
            print(
                f"Repaired: {len(before.missing)} rows rebuilt, "
                f"{len(before.orphans)} orphan rows removed."
            )
        else:
            print("Dry run: nothing was written. Re-run with --apply to repair.")
        return

    if action == "rate":
        try:
            record_feedback(output_dir, args.memory_id, args.rater, args.score)
            weight, count = feedback_weight(output_dir, args.memory_id)
        except ValueError as exc:
            print(f"Could not rate memory: {exc}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.OperationalError:
            print("Could not write memory: database is busy, try again", file=sys.stderr)
            sys.exit(1)
        print(f"Rated memory #{args.memory_id} {args.score}/5 (weight {weight:.2f}, n={count}).")
        return

    if action == "add":
        if args.kind not in MEMORY_KINDS:
            print(f"Invalid kind. Expected one of: {', '.join(MEMORY_KINDS)}", file=sys.stderr)
            sys.exit(1)
        supersedes = getattr(args, "supersedes", None)
        try:
            from lore.storage import find_similar_memories

            # Before the insert, so the new memory cannot match itself.
            similar = find_similar_memories(
                output_dir,
                f"{args.title}\n{args.body}",
                scope_project=args.project,
                exclude_id=supersedes,
            )
            memory_id = add_memory(
                output_dir,
                kind=args.kind,
                title=args.title,
                body=args.body,
                author=args.author or "human",
                scope_project=args.project,
                tags=args.tag or None,
                source_session=getattr(args, "from_session", None),
                supersedes=supersedes,
            )
        except ValueError as exc:
            print(f"Could not add memory: {exc}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.OperationalError:
            print("Could not write memory: database is busy, try again", file=sys.stderr)
            sys.exit(1)
        print(f"Stored memory #{memory_id} ({args.kind}).")
        if similar:
            listed = ", ".join(f"#{m['id']} {m['title']} ({m['similarity']:.2f})" for m in similar)
            print(f"Similar existing memories: {listed}")
        return

    if action == "list":
        entries = list_memory(
            output_dir,
            kind=args.kind,
            scope_project=args.project,
            include_superseded=args.all,
            limit=args.limit,
            status=getattr(args, "status", None) or "confirmed",
        )
    elif action == "search":
        entries = recall_memory(
            output_dir,
            args.query,
            kind=args.kind,
            scope_project=args.project,
            limit=args.limit,
            semantic=True if getattr(args, "semantic", False) else None,
        )
    else:  # pragma: no cover - argparse guards this
        print("Unknown memory action.", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        # The audit export (#187): each memory with its claim type, confidence,
        # evidence and source sessions, so a rule can be traced back.
        print(json.dumps([entry.to_dict() for entry in entries], indent=2))
        return

    if not entries:
        print("No memories found.")
        return

    for entry in entries:
        tag_str = f"  [{', '.join(entry.tags)}]" if entry.tags else ""
        scope = f" · {entry.scope_project}" if entry.scope_project else ""
        print(f"#{entry.id} ({entry.kind}) {entry.title}{tag_str}")
        print(f"   {entry.body}")
        claim = entry.claim
        if claim["type"]:
            confidence = (
                f" {claim['confidence']:.2f}"
                if isinstance(claim["confidence"], (int, float))
                else ""
            )
            print(f"   claim: {claim['type']}{confidence} · state: {entry.claim_state}")
            evidence = claim.get("evidence") or {}
            if evidence.get("session_id"):
                print(
                    f"   evidence: session {evidence['session_id']} message {evidence.get('message_id')}"
                )
        print(f"   — {entry.author or 'unknown'} · {entry.created[:10]}{scope}")
        print()


def cmd_threads(args):
    """List threads across sessions."""
    output_dir = Path(args.output_dir).expanduser()
    index_path = output_dir / "index.json"

    if not index_path.exists():
        print("No index found. Run 'lore export --all' first.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    by_thread = {}
    for s in filter_automated(index.get("sessions", []), _include_automated(args)):
        thread_id = s.get("thread_id")
        if not thread_id:
            continue
        if thread_id not in by_thread:
            by_thread[thread_id] = {
                "count": 0,
                "project": s.get("project"),
                "updated": s.get("updated"),
                "title": s.get("title") or s.get("id"),
            }
        by_thread[thread_id]["count"] += 1
        if s.get("updated", "") > by_thread[thread_id]["updated"]:
            by_thread[thread_id]["updated"] = s.get("updated")
            by_thread[thread_id]["title"] = s.get("title") or s.get("id")
            by_thread[thread_id]["project"] = s.get("project")

    threads = sorted(by_thread.items(), key=lambda kv: kv[1]["updated"], reverse=True)
    if not threads:
        print("No threads found.")
        return

    print(f"{'Thread ID':<45} {'Sessions':>8}  {'Project/Title'}")
    print("-" * 80)
    for thread_id, meta in threads:
        title = meta.get("title") or meta.get("project") or thread_id[-12:]
        if len(title) > 30:
            title = title[:27] + "..."
        print(f"{thread_id:<45} {meta['count']:>8}  {title}")


def cmd_rules(args):
    output_dir = Path(args.output_dir).expanduser()
    rules_path = output_dir / "rules.md"

    sessions = []
    for extractor in get_all_extractors():
        if not extractor.is_available():
            continue
        sessions.extend(extractor.extract_sessions())

    sessions = filter_automated(sessions, _include_automated(args))
    rules = extract_rules(sessions, max_rules=args.limit)
    if not rules:
        print("No rules extracted.")
        return

    lines = ["# Project Rules", ""]
    for rule in rules:
        lines.append(f"- {rule}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Rules saved to: {rules_path}")


def cmd_generate_titles(args):
    from lore.titles import TitleGenerator, TitleStrategy

    strategy_map = {
        "fast": TitleStrategy.FAST,
        "keyword": TitleStrategy.KEYWORD,
        "smart": TitleStrategy.SMART,
        "auto": TitleStrategy.AUTO,
    }

    strategy = strategy_map.get(args.strategy, TitleStrategy.AUTO)
    provider = getattr(args, "provider", None)
    # Privacy by default (#185): the local provider needs no opt-in; any
    # non-local provider (gemini-cli, or an off-host ollama behind "local")
    # sends a transcript excerpt and is gated like analyze/knowledge/format.
    gate_provider = "ollama" if (provider or "local") == "local" else provider
    from lore.services.summarize_cli import require_cloud_opt_in

    allow_cloud = bool(getattr(args, "allow_cloud", False))
    if (rc := require_cloud_opt_in(gate_provider, allow_cloud)) is not None:
        return rc
    generator = TitleGenerator(
        strategy=strategy,
        provider=provider,
        model=getattr(args, "model", None),
        allow_cloud=allow_cloud,
    )
    if strategy in (TitleStrategy.SMART, TitleStrategy.AUTO):
        # Titles send a transcript excerpt: say where they go before the first
        # call, so an explicit provider selection is never silent (#185).
        print(f"Title provider: {generator.provider} (model: {generator.model})")

    extractors = get_all_extractors()
    generated = 0
    skipped = 0

    tool_filter = normalize_tool_name(args.tool) if args.tool else None

    for extractor in extractors:
        if tool_filter and extractor.tool.value != tool_filter:
            continue
        if not extractor.is_available():
            continue

        print(f"\nProcessing {extractor.tool.value}...")

        for session in extractor.extract_sessions():
            if not _include_automated(args) and session.origin in AUTOMATED_ORIGINS:
                continue
            if args.since:
                cutoff = datetime.now() - parse_duration(args.since)
                if make_naive(session.created_at) < cutoff:
                    skipped += 1
                    continue

            title = generator.generate(session, force=args.force)
            source = session.title_source.value if session.title_source else "unknown"
            print(f"  [{source}] {session.session_id[:12]}... → {title}")
            generated += 1

    print(f"\n✅ Generated: {generated} | ⏭️ Skipped: {skipped}")


def cmd_watch(args):
    """Watch for new sessions and auto-export."""
    from lore.watcher import SessionWatcher

    output_dir = Path(args.output_dir).expanduser()
    exporter = MarkdownExporter(output_dir)
    index_builder = IndexBuilder(output_dir)

    print(f"Watching for new sessions (interval: {args.interval}s)")
    print("Press Ctrl+C to stop.\n")

    # Track known session IDs
    known_sessions = set()

    # Initial scan
    extractors = get_all_extractors()
    for extractor in extractors:
        if not extractor.is_available():
            continue
        for session in extractor.extract_sessions():
            known_sessions.add(session.session_id)

    print(f"Found {len(known_sessions)} existing sessions.\n")

    watcher = SessionWatcher(callback=lambda: None, interval=args.interval)
    # Seed the watcher snapshot so the first poll only reports real changes.
    watcher.poll_once()

    while True:
        try:
            time.sleep(args.interval)

            changed_paths = watcher.poll_once()
            for path in changed_paths:
                print(f"Changed: {path}")

            new_sessions = []
            for extractor in extractors:
                if not extractor.is_available():
                    continue
                for session in extractor.extract_sessions():
                    if session.session_id not in known_sessions:
                        new_sessions.append(session)
                        known_sessions.add(session.session_id)

            if new_sessions:
                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] Found {len(new_sessions)} new session(s)"
                )

                export_paths = {}
                for session in new_sessions:
                    try:
                        path = exporter.export_session(session)
                        export_paths[session.session_id] = path
                        print(
                            f"  Exported: {session.tool.value} - {session.title or session.session_id[:8]}"
                        )
                    except Exception as e:
                        print(f"  Error: {e}", file=sys.stderr)

                # Update index
                all_sessions = []
                for extractor in extractors:
                    if not extractor.is_available():
                        continue
                    all_sessions.extend(extractor.extract_sessions())

                existing_paths, _ = _load_existing_index_state(output_dir / "index.json")

                merged_paths = {**existing_paths, **export_paths}
                index_builder.build_index(all_sessions, merged_paths)

                # Git commit if requested
                if args.git:
                    try:
                        subprocess.run(
                            ["git", "-C", str(output_dir), "add", "."],
                            capture_output=True,
                            check=True,
                            shell=False,
                            timeout=30,
                        )
                        msg = f"Auto-export: {len(new_sessions)} new session(s)"
                        subprocess.run(
                            ["git", "-C", str(output_dir), "commit", "-m", msg],
                            capture_output=True,
                            check=True,
                            shell=False,
                            timeout=30,
                        )
                        print(f"  Git commit: {msg}")
                    except subprocess.CalledProcessError:
                        pass  # Not a git repo or nothing to commit
                    except subprocess.TimeoutExpired:
                        print("  Git operation timed out", file=sys.stderr)

        except KeyboardInterrupt:
            print("\nStopped watching.")
            break


def _format_skips(skips: dict) -> str:
    return ", ".join(f"{reason}={n}" for reason, n in sorted(skips.items()))


def _print_deep_report(extractor) -> bool:
    """Run one extractor to completion, write nothing, print its counters.

    Returns False when the extractor raised.
    """
    tool = extractor.tool.value
    try:
        sessions = sum(1 for _ in extractor.extract_sessions())
    except Exception as exc:  # an extractor bug must not hide the other tools
        print(f"  ERROR: {tool}: extraction failed ({type(exc).__name__})", file=sys.stderr)
        return False
    found = extractor.discovery
    print(f"  Sources matched: {found['files_matched']}")
    print(f"  Sessions parsed: {sessions}")
    print(f"  Skipped: {_format_skips(extractor.skip_counts) or 'none'}")
    print(f"  Unknown records: {found['records_unknown']}")
    print(f"  Parsed files: {found['files_parsed']}")
    # Extractors may report finer coverage (Antigravity splits transcripts from
    # task artifacts and undecodable blobs, #158); print whatever they added.
    from lore.extractors.base import DISCOVERY_KEYS

    extra = {key: value for key, value in found.items() if key not in DISCOVERY_KEYS and value}
    if extra:
        print(f"  Coverage: {', '.join(f'{key}={value}' for key, value in sorted(extra.items()))}")
    if sessions == 0 and found["files_matched"] > 0:
        print(
            f"WARNING: {tool}: {found['files_matched']} source files matched "
            f"{extractor.source_description()} but 0 sessions were produced "
            "(stale glob or format change?)"
        )
    elif sessions == 0:
        # An available tool has a data directory; no source matched the glob.
        print(
            f"WARNING: {tool}: 0 source files matched {extractor.source_description()} "
            "although its data directory exists (stale glob or format change?)"
        )
    return True


def _print_last_sync(last: dict) -> None:
    print(
        f"  Last sync {last.get('recorded_at', '?')}: "
        f"{last.get('files_matched', 0)} sources matched, "
        f"{last.get('files_parsed', 0)} files parsed, "
        f"{last.get('sessions', 0)} sessions, "
        f"{last.get('records_unknown', 0)} unknown records"
        + (f", skipped {_format_skips(last['skip_counts'])}" if last.get("skip_counts") else "")
    )


def cmd_check(args):
    """Check availability of AI tools (``--deep``: run every extractor, write nothing)."""
    deep = bool(getattr(args, "deep", False))
    print("Checking AI tools availability...")
    print("-" * 40)

    extractors = get_all_extractors()
    available_count = 0
    failed = False
    last_runs = {}
    if not deep:
        output_dir = getattr(args, "output_dir", None) or lore_home()
        last_runs = read_last_discovery(Path(output_dir).expanduser())

    for extractor in extractors:
        available = extractor.is_available()
        status = "✅ Available" if available else "❌ Not found"
        print(f"{extractor.tool.value:<20} {status}")

        if available:
            available_count += 1
            # Basic path check info
            base_path = getattr(extractor, "base_path", None)
            db_path = getattr(extractor, "db_path", None)
            if base_path:
                print(f"  Path: {base_path}")
            elif db_path:
                print(f"  DB: {db_path}")
            if deep:
                failed = not _print_deep_report(extractor) or failed
            elif extractor.tool.value in last_runs:
                _print_last_sync(last_runs[extractor.tool.value])
        elif deep and extractor.has_source_data():
            # A data directory with nothing readable still has a story to tell:
            # report its coverage counters instead of only "not found" (#158).
            print(f"  {extractor.tool.value}: data directory present, no readable input")
            failed = not _print_deep_report(extractor) or failed

    print("-" * 40)
    print(f"Summary: {available_count}/{len(extractors)} tools available.")
    from lore.utils.claude_retention import retention_check_lines

    for line in retention_check_lines():
        print(line)
    if failed:
        sys.exit(1)


def _run_export_html(args) -> None:
    # Historically the only command whose return value feeds sys.exit.
    sys.exit(cmd_export_html(args) or 0)


# Single source of truth for command dispatch (#118): every subparser added
# in main() must have an entry here and vice versa — enforced by
# tests/test_cli_dispatch.py so the two can never drift apart again.
COMMANDS = {
    "list": cmd_list,
    "export": cmd_export,
    "export-html": _run_export_html,
    "search": cmd_search,
    "stats": cmd_stats,
    "digest": cmd_digest,
    "memory": cmd_memory,
    "watch": cmd_watch,
    "check": cmd_check,
    "threads": cmd_threads,
    "rules": cmd_rules,
    "prune": cmd_prune,
    "sync": cmd_sync,
    "run": cmd_run,
    "generate-titles": cmd_generate_titles,
    "reindex": cmd_reindex,
    "analyze": cmd_analyze,
    "knowledge": cmd_knowledge,
    "improve": cmd_improve,
    "summarize": cmd_summarize,
    "format": cmd_format,
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level ``lore`` argument parser.

    Split out of main() so tests can introspect the registered subparsers
    and pin them against the COMMANDS dispatch table (#118).
    """
    parser = argparse.ArgumentParser(
        prog="lore",
        description="Lore — local-first archive and shared agent memory for your AI coding sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  lore list                      List all sessions
  lore list --tool claude-code   List Claude Code sessions only
  lore export --all              Export all sessions to ~/.lore
  lore search "docker"           Search for "docker" in all sessions
  lore watch --git               Watch for new sessions and auto-commit
  lore check                     Check availability of AI tools
        """,
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    from lore import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"lore {__version__}",
        help="Show the lore version and exit",
    )
    parser.add_argument(
        "--output-dir",
        default=str(lore_home()),
        help="Output directory (default: ~/.lore)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # list command
    list_parser = subparsers.add_parser("list", help="List all sessions")
    list_parser.add_argument("--tool", help="Filter by tool")
    list_parser.add_argument("--project", help="Filter by project path")
    list_parser.add_argument(
        "--tag", help="Filter by a saved tag/bookmark (session_tags) or an index keyword"
    )
    list_parser.add_argument("--since", help="Filter by date (e.g., 7d, 2w)")
    list_parser.add_argument("--format", choices=["table", "json"], default="table")
    list_parser.add_argument("--limit", type=int, help="Limit number of results")
    list_parser.add_argument("--offset", type=int, default=0, help="Offset for pagination")
    _add_automated_flag(list_parser)

    # export command
    export_parser = subparsers.add_parser("export", help="Export sessions to Markdown")
    export_parser.add_argument("--all", action="store_true", help="Export all sessions")
    export_parser.add_argument("--tool", help="Export only specific tool")
    export_parser.add_argument("--project", help="Export only specific project")
    export_parser.add_argument(
        "--full",
        action="store_true",
        help="Re-parse every source, even those unchanged since the last export",
    )
    export_parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact secret-shaped strings (requires a non-default --output-dir)",
    )

    # export-html command — single shareable standalone HTML file
    export_html_parser = subparsers.add_parser(
        "export-html", help="Export one session as a standalone HTML file"
    )
    export_html_parser.add_argument("session_id", help="Session ID (full or unique prefix)")
    export_html_parser.add_argument(
        "--output",
        help="Output file or directory (default: <session-id>.html in cwd)",
    )
    export_html_parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact secret-shaped strings before rendering HTML",
    )

    # search command
    search_parser = subparsers.add_parser("search", help="Search sessions")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--tool", help="Search only specific tool")
    search_parser.add_argument("--project", help="Search only specific project")
    search_parser.add_argument("--tag", help="Search only sessions with this saved tag/bookmark")
    search_parser.add_argument(
        "--page", type=int, default=1, help="Page of results (20 per page; default 1)"
    )
    _add_automated_flag(search_parser)

    # stats command
    _add_automated_flag(subparsers.add_parser("stats", help="Show statistics"))

    # digest command
    digest_parser = subparsers.add_parser(
        "digest", help="Print an activity digest for a recent time window"
    )
    digest_parser.add_argument(
        "--since",
        default="7d",
        help="Time window to summarize (e.g., 7d, 2w, 1m; default: 7d)",
    )
    digest_parser.add_argument(
        "--format",
        choices=["text", "markdown"],
        default="text",
        help="Output format (default: text)",
    )
    _add_automated_flag(digest_parser)

    # memory command — shared cross-tool knowledge store (#44 / #33)
    memory_parser = subparsers.add_parser(
        "memory", help="Manage the shared cross-tool memory store"
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_action", required=True)

    mem_add = memory_sub.add_parser("add", help="Record a new memory")
    mem_add.add_argument(
        "--kind",
        default="note",
        help="fact|decision|todo|snippet|link|lesson|note (default: note)",
    )
    mem_add.add_argument("--title", required=True, help="short memory title")
    mem_add.add_argument("--body", required=True, help="the memory content")
    mem_add.add_argument("--project", help="optional project scope")
    mem_add.add_argument("--author", help="who recorded it (default: human)")
    mem_add.add_argument("--tag", action="append", help="tag (repeatable: --tag db --tag infra)")
    mem_add.add_argument(
        "--from-session", help="id of the session this memory came from (provenance)"
    )
    mem_add.add_argument(
        "--supersedes",
        type=int,
        metavar="ID",
        help="id of an active, confirmed memory (same project) that this one replaces",
    )

    mem_list = memory_sub.add_parser("list", help="List recorded memories")
    mem_list.add_argument("--kind", help="filter by kind")
    mem_list.add_argument("--project", help="filter by project scope")
    mem_list.add_argument("--all", action="store_true", help="include superseded memories")
    mem_list.add_argument("--limit", type=int, default=50, help="max results")
    mem_list.add_argument(
        "--status",
        choices=["confirmed", "candidate", "rejected"],
        help="filter by review status (default: confirmed)",
    )
    mem_list.add_argument(
        "--json", action="store_true", help="full audit export: claim, evidence and sources"
    )

    mem_search = memory_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query", help="search query")
    mem_search.add_argument("--kind", help="filter by kind")
    mem_search.add_argument("--project", help="filter by project scope")
    mem_search.add_argument("--limit", type=int, default=10, help="max results")
    mem_search.add_argument(
        "--semantic",
        action="store_true",
        help="rank by meaning only (default: keywords plus meaning when the "
        "'semantic' extra is installed)",
    )

    mem_dedupe = memory_sub.add_parser(
        "dedupe", help="Reject exact confirmed memory duplicates (dry run unless --apply)"
    )
    mem_dedupe.add_argument(
        "--apply", action="store_true", help="mark duplicates rejected (default: dry run)"
    )
    mem_dedupe.add_argument(
        "--restore",
        metavar="FILE",
        help="restore rejections from a dedupe undo log written by --apply",
    )

    mem_import = memory_sub.add_parser(
        "import-claude",
        help="Import Claude Code auto-memory files (dry run unless --apply)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Seed the memory store from ~/.claude/projects/<slug>/memory/*.md.\n\n"
            "Type mapping: user -> fact, feedback -> lesson, project -> fact, "
            "reference -> link.\n"
            "Files with a missing or unknown type, or no usable name/description, are\n"
            "imported as candidates (kind note). MEMORY.md indexes, files without\n"
            "frontmatter and empty bodies are skipped.\n\n"
            "Re-running writes nothing for unchanged files; an edited file is written as\n"
            "a new memory that supersedes the previous import. Titles and bodies are\n"
            "redacted before writing."
        ),
    )
    mem_import.add_argument(
        "--apply", action="store_true", help="write to the store (default: dry run)"
    )
    mem_import.add_argument(
        "--root", help="Claude projects directory (default: ~/.claude/projects)"
    )

    mem_show = memory_sub.add_parser("show", help="Show one memory in full")
    mem_show.add_argument("memory_id", type=int, help="memory id")

    mem_context = memory_sub.add_parser(
        "context", help="Render a budgeted memory block for agent context"
    )
    mem_context.add_argument(
        "--project",
        help="project scope (default: current working directory, resolved)",
    )
    mem_context.add_argument(
        "--budget",
        type=int,
        default=2000,
        help="token budget estimate (ceil(chars/4); default: 2000)",
    )
    mem_context.add_argument("--tool", help="optional tool scope filter")
    mem_context.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    mem_context.add_argument("--limit", type=int, default=40, help="max memories to consider")

    mem_review = memory_sub.add_parser(
        "review", help="Review candidates: list them, or accept/reject by id"
    )
    mem_review.add_argument(
        "target",
        nargs="?",
        choices=["accept", "reject"],
        help="omit to list the candidates waiting for review",
    )
    mem_review.add_argument("ids", nargs="*", help="candidate ids to accept or reject")
    mem_review.add_argument("--kind", help="filter the listing by kind")
    mem_review.add_argument("--json", action="store_true", help="print the result as JSON")

    mem_repair = memory_sub.add_parser(
        "repair-index",
        help="Detect and rebuild missing memory search rows (dry run by default)",
    )
    mem_repair.add_argument(
        "--apply", action="store_true", help="write the repair instead of only reporting it"
    )
    mem_repair.add_argument("--json", action="store_true", help="print the report as JSON")

    mem_rate = memory_sub.add_parser("rate", help="Rate a memory from 1 to 5")
    mem_rate.add_argument("memory_id", type=int, help="memory id")
    mem_rate.add_argument("score", type=int, help="1 (wrong) to 5 (right and useful)")
    mem_rate.add_argument("--rater", default="human", help="who rates (default: human)")

    # watch command
    watch_parser = subparsers.add_parser("watch", help="Watch for new sessions")
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (default: 60)",
    )
    watch_parser.add_argument("--git", action="store_true", help="Auto-commit new exports to git")

    # check command
    check_parser = subparsers.add_parser("check", help="Check tool availability")
    check_parser.add_argument(
        "--deep",
        action="store_true",
        help="Run every available extractor (writes nothing) and report matched sources, "
        "sessions, skips and unknown records",
    )

    # threads command
    _add_automated_flag(subparsers.add_parser("threads", help="List threads"))

    # rules command
    rules_parser = subparsers.add_parser("rules", help="Generate derived rules")
    rules_parser.add_argument("--limit", type=int, default=30, help="Max rules to extract")
    _add_automated_flag(rules_parser)

    # prune command
    prune_parser = subparsers.add_parser("prune", help="Prune sessions by prompt count")
    prune_parser.add_argument(
        "--max-prompts",
        type=int,
        default=3,
        help="Delete sessions with this many prompts or fewer",
    )
    prune_parser.add_argument("--dry-run", action="store_true", help="Show what would be pruned")
    _add_automated_flag(prune_parser)

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Sync sessions for a tool")
    sync_parser.add_argument("tool", help="Tool to sync (e.g., codex, claude-code)")
    sync_parser.add_argument("--session-id", help="Sync only a specific session ID")
    sync_parser.add_argument("--project", help="Sync only a specific project")

    # run command
    run_parser = subparsers.add_parser("run", help="Run a tool and sync after it exits")
    run_parser.add_argument("tool", help="Tool to run (e.g., codex, claude-code)")
    run_parser.add_argument("tool_args", nargs=argparse.REMAINDER, help="Args passed to the tool")
    run_parser.add_argument("--no-pty", action="store_true", help="Disable PTY capture")

    # generate-titles command
    titles_parser = subparsers.add_parser(
        "generate-titles", help="Generate readable titles for sessions"
    )
    titles_parser.add_argument(
        "--strategy",
        choices=["fast", "keyword", "smart", "auto"],
        default="auto",
        help="Title generation strategy (default: auto)",
    )
    titles_parser.add_argument("--tool", help="Only generate for specific tool")
    titles_parser.add_argument("--since", help="Only sessions since (e.g., 7d, 2w)")
    titles_parser.add_argument("--force", action="store_true", help="Regenerate even if cached")
    titles_parser.add_argument(
        "--provider",
        choices=["local", "gemini-cli"],
        default=None,
        help=(
            "Provider for LLM titles (default: local). 'gemini-cli' is a cloud "
            "provider and is only used when selected here — titles send a transcript excerpt."
        ),
    )
    titles_parser.add_argument(
        "--model", default=None, help="Model for the selected provider (provider default otherwise)"
    )
    titles_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive session text (or set LORE_ALLOW_CLOUD=1)",
    )
    _add_automated_flag(titles_parser)

    subparsers.add_parser(
        "reindex",
        help="Rebuild index from all sources on disk (fixes missing sessions after sync)",
    )

    # analyze command - LLM-powered statistics
    analyze_parser = subparsers.add_parser(
        "analyze", help="Generate AI-powered statistics and insights"
    )
    analyze_parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default="gemini",
        help="LLM provider to use (default: gemini)",
    )
    analyze_parser.add_argument("--model", help="Model name (default depends on --provider)")
    analyze_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive session text (or set LORE_ALLOW_CLOUD=1)",
    )
    analyze_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Generate basic stats without LLM",
    )
    analyze_parser.add_argument(
        "--output",
        help="Output file for stats (default: ~/.lore/stats/stats.json)",
    )
    _add_automated_flag(analyze_parser)

    # knowledge command - Build knowledge database
    knowledge_parser = subparsers.add_parser(
        "knowledge", help="Extract knowledge from sessions using LLM"
    )
    knowledge_parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help=(
            "LLM provider (required; no default — bare lore knowledge sends nothing). "
            "Use ollama for local, or a cloud provider with --allow-cloud"
        ),
    )
    knowledge_parser.add_argument("--model", help="Model name (default depends on --provider)")
    knowledge_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive session text (or set LORE_ALLOW_CLOUD=1)",
    )
    knowledge_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max sessions to process (default: 50)",
    )
    knowledge_parser.add_argument("--tool", help="Only process sessions from specific tool")
    knowledge_parser.add_argument(
        "--output",
        help="Output file (default: ~/.lore/knowledge/knowledge_base.json)",
    )
    _add_automated_flag(knowledge_parser)

    improve_parser = subparsers.add_parser(
        "improve",
        help="Backfill embeddings, extract knowledge, and propose memory candidates",
    )
    improve_parser.add_argument("--provider", help="LLM provider for extract_knowledge")
    improve_parser.add_argument("--model", help="Model name for extract_knowledge")
    improve_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive session text (or set LORE_ALLOW_CLOUD=1)",
    )
    improve_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max sessions for extract_knowledge (default: 50)",
    )
    improve_parser.add_argument(
        "--stage",
        action="append",
        choices=["embed_memories", "extract_knowledge", "distill_lessons", "from_summaries"],
        help="Run only these stages (repeatable)",
    )
    improve_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute candidate counts without writing or calling an LLM",
    )
    improve_parser.add_argument("--json", action="store_true", help="Print stage report as JSON")
    _add_automated_flag(improve_parser)

    summarize_parser = subparsers.add_parser(
        "summarize", help="Generate AI session summaries for archived sessions"
    )
    summarize_parser.add_argument("--session", help="Session ID (full or unique prefix)")
    summarize_parser.add_argument("--project", help="Only sessions for this project path")
    summarize_parser.add_argument("--tool", help="Only sessions from this tool")
    summarize_parser.add_argument(
        "--since",
        help="Only sessions active since this duration (default 30d when --session is omitted)",
    )
    summarize_parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default="ollama",
        help="LLM provider (default: ollama, local)",
    )
    summarize_parser.add_argument("--model", help="Model name (default depends on --provider)")
    summarize_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive transcript text (or set LORE_ALLOW_CLOUD=1)",
    )
    summarize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate tokens and calls without contacting a provider",
    )
    summarize_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-summarize even when the fingerprint is unchanged",
    )
    summarize_parser.add_argument(
        "--max-sessions",
        type=int,
        help="Maximum number of sessions to consider",
    )
    summarize_parser.add_argument(
        "--max-input-tokens",
        type=int,
        help="Stop after this many estimated input tokens (real run)",
    )
    summarize_parser.add_argument(
        "--max-session-tokens",
        type=int,
        help="Skip sessions whose compressed transcript exceeds this token budget",
    )
    summarize_parser.add_argument(
        "--level",
        choices=["session", "day", "project"],
        default="session",
        help="Summary granularity (only session is implemented)",
    )
    summarize_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON on stdout",
    )
    _add_automated_flag(summarize_parser)

    # format command - Format sessions with LLM
    format_parser = subparsers.add_parser(
        "format", help="Format sessions with AI-generated summaries and tags"
    )
    format_parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default="gemini",
        help="LLM provider to use (default: gemini)",
    )
    format_parser.add_argument("--model", help="Model name (default depends on --provider)")
    format_parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Allow cloud providers to receive session text (or set LORE_ALLOW_CLOUD=1)",
    )
    format_parser.add_argument("--session-id", help="Format specific session by ID")
    _add_automated_flag(format_parser)
    format_parser.add_argument("--tool", help="Format sessions from specific tool")
    format_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max sessions to format (default: 10)",
    )
    format_parser.add_argument(
        "--output-dir",
        help="Output directory (default: ~/.lore/formatted)",
    )

    return parser


def main():
    make_console_safe()
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return
    rc = handler(args)
    if isinstance(rc, int) and rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()
