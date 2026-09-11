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
- Warp Terminal
- Codex CLI

Usage:
    lore list [--tool TOOL] [--since DURATION] [--format FORMAT]
    lore export [--all] [--tool TOOL] [--project PATH]
    lore search QUERY [--tool TOOL] [--context N]
    lore stats
    lore digest [--since 7d] [--format text|markdown]
    lore memory add|list|search ...
    lore watch [--interval SECS] [--git]
    lore check
    lore sync TOOL [--session-id ID] [--project PATH]
    lore run TOOL [-- ARGS...]
    lore threads
"""

import argparse
import json
import os
import pty
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add local package to path if running from source
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lore.core.models import Tool, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.exporters.markdown import MarkdownExporter
from lore.extractors.factory import get_all_extractors
from lore.extractors.opencode import OpenCodeExtractor
from lore.utils.datetime import make_naive, parse_duration
from lore.utils.git import get_git_info
from lore.utils.paths import get_current_project, lore_home, make_thread_id
from lore.utils.rules import extract_rules
from lore.utils.tooling import normalize_tool_name

TOOL_EXECUTABLES = {
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "codex": "codex",
    "warp": "warp",
}


def _get_extractors_for_tool(tool: str):
    extractors = get_all_extractors()
    if not tool:
        return extractors
    tool = normalize_tool_name(tool) or tool
    return [ex for ex in extractors if ex.tool.value == tool]


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
        if _is_low_value_index_entry(entry):
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

    # Sort by date
    sessions.sort(key=lambda s: make_naive(s.last_updated), reverse=True)

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

    if limit:
        print(f"\nShowing {len(sessions)} of {total_sessions} sessions (offset: {offset})")
        if offset + limit < total_sessions:
            print(f"  → Use --offset {offset + limit} --limit {limit} to see more")
    else:
        print(f"\nTotal: {total_sessions} sessions")


def cmd_export(args):
    """Export sessions to Markdown."""
    output_dir = Path(args.output_dir).expanduser()
    exporter = MarkdownExporter(output_dir)
    index_builder = IndexBuilder(output_dir)

    extractors = get_all_extractors()
    sessions = []
    export_paths = {}

    tool_filter = normalize_tool_name(args.tool) if args.tool else None

    for extractor in extractors:
        if tool_filter and extractor.tool.value != tool_filter:
            continue
        if not extractor.is_available():
            continue

        print(f"Extracting from {extractor.tool.value}...")

        for session in extractor.extract_sessions():
            # Filter by project
            if args.project and session.project_path != args.project:
                continue

            # Tag session with current git branch/SHA of its project directory
            if session.git_branch is None or session.git_commit is None:
                info = get_git_info(session.project_path)
                if session.git_branch is None:
                    session.git_branch = info["branch"]
                if session.git_commit is None:
                    session.git_commit = info["sha"]

            sessions.append(session)

            # Export to markdown (skips if file is already up-to-date)
            try:
                candidate = exporter._candidate_path(session)
                was_current = (
                    candidate.exists()
                    and candidate.stat().st_mtime >= session.last_updated.timestamp()
                )
                path = exporter.export_session(session)
                export_paths[session.session_id] = path
                if not was_current:
                    print(f"  Exported: {path}")
            except Exception as e:
                print(f"  Error exporting {session.session_id}: {e}", file=sys.stderr)

    # Build index. When no --tool/--project filter is active, the sessions
    # already collected by the export loop above ARE the full set — re-running
    # every extractor here would double the I/O, CPU and memory on a full
    # sync (1885 sessions × 10 tools easily exhausts a 38 GiB box).
    print("\nBuilding index...")
    if not tool_filter and not args.project:
        all_sessions = sessions
    else:
        all_sessions = []
        git_info_cache = {}
        for extractor in get_all_extractors():
            if not extractor.is_available():
                continue
            for session in extractor.extract_sessions():
                if session.git_branch is None or session.git_commit is None:
                    key = session.project_path or ""
                    if key not in git_info_cache:
                        git_info_cache[key] = get_git_info(session.project_path)
                    info = git_info_cache[key]
                    if session.git_branch is None:
                        session.git_branch = info["branch"]
                    if session.git_commit is None:
                        session.git_commit = info["sha"]
                all_sessions.append(session)

    existing_paths, _ = _load_existing_index_state(output_dir / "index.json")

    merged_paths = {**existing_paths, **export_paths}
    index_builder.build_index(all_sessions, merged_paths)
    print(f"Index saved to: {index_builder.index_path}")

    print(f"\nExported {len(sessions)} sessions to {output_dir}")


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


def cmd_prune(args):
    """Prune sessions from the export/index based on prompt count."""
    output_dir = Path(args.output_dir).expanduser()
    index_path = output_dir / "index.json"
    if not index_path.exists():
        print(f"No index found at {index_path}. Run `lore export --all` first.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    candidates = [
        s
        for s in index.get("sessions", [])
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

    ignore_path = output_dir / "ignored.json"
    ignored = set()
    if ignore_path.exists():
        try:
            data = json.loads(ignore_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ignored.update(data.get("session_ids", []))
            else:
                ignored.update(data)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    removed = []
    for s in candidates:
        session_id = s.get("id")
        ignored.add(session_id)
        removed.append(session_id)
        export_path = s.get("export_path")
        if export_path:
            try:
                path = Path(export_path)
                if path.exists():
                    path.unlink()
            except (OSError, PermissionError):
                pass

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

    IndexBuilder(output_dir).build_index(sessions, export_paths)
    print(f"Pruned {len(removed)} sessions and rebuilt index.")


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
        "warp",
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

    sessions = [_index_entry_to_session(s) for s in index.get("sessions", [])]

    print(f"Analyzing {len(sessions)} sessions...")

    provider = None
    if not args.no_llm:
        try:
            config = LLMConfig(
                provider=args.provider,
                model=args.model
                or ("gemini-2.0-flash" if args.provider == "gemini" else "llama3.2"),
            )
            provider = get_provider(
                provider=args.provider,
                model=config.model,
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


def cmd_knowledge(args):
    """Extract knowledge from sessions using LLM."""
    from lore.llm import LLMConfig, get_provider
    from lore.llm.tasks import KnowledgeExtractor

    output_dir = Path(args.output_dir).expanduser()
    index_file = output_dir / "index.json"

    if not index_file.exists():
        print("Error: No index found. Run 'lore reindex' first.", file=sys.stderr)
        return 1

    with open(index_file, "r") as f:
        index = json.load(f)

    sessions = [_index_entry_to_session(s) for s in index.get("sessions", [])]

    if args.tool:
        tool = normalize_tool_name(args.tool) or args.tool
        sessions = [s for s in sessions if s.tool.value == tool]

    sessions = sorted(sessions, key=lambda s: s.created_at, reverse=True)[: args.limit]

    print(f"Extracting knowledge from {len(sessions)} sessions...")

    try:
        config = LLMConfig(
            provider=args.provider,
            model=args.model or ("gemini-2.0-flash" if args.provider == "gemini" else "llama3.2"),
        )
        provider = get_provider(
            provider=args.provider,
            model=config.model,
        )

        if not provider.is_available():
            print(f"Error: {args.provider} not available. Set GEMINI_API_KEY or GOOGLE_API_KEY.")
            return 1

    except Exception as e:
        print(f"Error: Could not initialize LLM: {e}")
        return 1

    extractor = KnowledgeExtractor(provider, output_dir / "knowledge")
    entries = extractor.extract_from_sessions(sessions)

    if not entries:
        print("No knowledge entries extracted.")
        return 1

    output_file = Path(args.output) if args.output else extractor.output_dir / "knowledge_base.json"
    extractor.build_knowledge_base(entries, output_file.name)

    print(f"\nKnowledge base saved to: {output_file}")
    print(f"Extracted {len(entries)} entries")

    # Show sample
    if entries:
        print("\nSample entry:")
        print(f"  Topic: {entries[0].topic}")
        print(f"  Key points: {', '.join(entries[0].key_points[:3])}")


def cmd_format(args):
    """Format sessions with AI-generated summaries and tags."""
    from lore.llm import LLMConfig, get_provider
    from lore.llm.tasks import SessionFormatter

    output_dir = Path(args.output_dir).expanduser()
    index_file = output_dir / "index.json"

    if not index_file.exists():
        print("Error: No index found. Run 'lore reindex' first.", file=sys.stderr)
        return 1

    with open(index_file, "r") as f:
        index = json.load(f)

    sessions = [_index_entry_to_session(s) for s in index.get("sessions", [])]

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
        config = LLMConfig(
            provider=args.provider,
            model=args.model or ("gemini-2.0-flash" if args.provider == "gemini" else "llama3.2"),
        )
        provider = get_provider(
            provider=args.provider,
            model=config.model,
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
    for extractor in get_all_extractors():
        if not extractor.is_available():
            continue
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
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    start_ts = datetime.now()
    log_name = f"{start_ts.strftime('%Y%m%d_%H%M%S')}_{tool}.log"
    log_path = runs_dir / log_name

    project_path = get_current_project()
    thread_id = make_thread_id(project_path=project_path)

    def run_with_pty() -> int:
        log_file = open(log_path, "ab")

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
            exit_status = run_with_pty()
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
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

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

    index_path = output_dir / "index.json"
    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    results = search_index(index_path, args.query, deleted, tool=args.tool, project=args.project)

    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} results for '{args.query}':\n")

    for r in results[:20]:  # Show top 20
        session = r["session"]
        score = r["score"]
        print(f"[{session['tool']}] {session.get('title', session['id'][:20])}")
        print(
            f"  Score: {score} | Date: {session['created'][:10]} | Messages: {session['messages']}"
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

    stats = index.get("stats", {})

    print("AI History Statistics")
    print("=" * 40)
    print(f"Total Sessions: {stats.get('total_sessions', 0)}")
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

    digest = build_digest(index.get("sessions", []), since=cutoff)
    fmt = "markdown" if args.format == "markdown" else "text"
    print(format_digest(digest, fmt=fmt))


def cmd_memory(args):
    """Manage the shared cross-tool memory store (issue #44 / #33)."""
    from lore.storage import MEMORY_KINDS, add_memory, list_memory, recall_memory

    output_dir = Path(args.output_dir).expanduser()
    action = args.memory_action

    if action == "add":
        if args.kind not in MEMORY_KINDS:
            print(f"Invalid kind. Expected one of: {', '.join(MEMORY_KINDS)}", file=sys.stderr)
            sys.exit(1)
        try:
            memory_id = add_memory(
                output_dir,
                kind=args.kind,
                title=args.title,
                body=args.body,
                author=args.author or "human",
                scope_project=args.project,
                tags=args.tag or None,
                source_session=getattr(args, "from_session", None),
            )
        except ValueError as exc:
            print(f"Could not add memory: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Stored memory #{memory_id} ({args.kind}).")
        return

    if action == "list":
        entries = list_memory(
            output_dir,
            kind=args.kind,
            scope_project=args.project,
            include_superseded=args.all,
            limit=args.limit,
        )
    elif action == "search":
        entries = recall_memory(
            output_dir,
            args.query,
            kind=args.kind,
            scope_project=args.project,
            limit=args.limit,
            semantic=getattr(args, "semantic", False),
        )
    else:  # pragma: no cover - argparse guards this
        print("Unknown memory action.", file=sys.stderr)
        sys.exit(1)

    if not entries:
        print("No memories found.")
        return

    for entry in entries:
        tag_str = f"  [{', '.join(entry.tags)}]" if entry.tags else ""
        scope = f" · {entry.scope_project}" if entry.scope_project else ""
        print(f"#{entry.id} ({entry.kind}) {entry.title}{tag_str}")
        print(f"   {entry.body}")
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
    for s in index.get("sessions", []):
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
    generator = TitleGenerator(strategy=strategy)

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


def cmd_check(args):
    """Check availability of AI tools."""
    print("Checking AI tools availability...")
    print("-" * 40)

    extractors = get_all_extractors()
    available_count = 0

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

    print("-" * 40)
    print(f"Summary: {available_count}/{len(extractors)} tools available.")


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
        help="Output directory (default: ~/.lore — migrates from ~/.ai-history if present)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # list command
    list_parser = subparsers.add_parser("list", help="List all sessions")
    list_parser.add_argument("--tool", help="Filter by tool")
    list_parser.add_argument("--project", help="Filter by project path")
    list_parser.add_argument("--since", help="Filter by date (e.g., 7d, 2w)")
    list_parser.add_argument("--format", choices=["table", "json"], default="table")
    list_parser.add_argument("--limit", type=int, help="Limit number of results")
    list_parser.add_argument("--offset", type=int, default=0, help="Offset for pagination")

    # export command
    export_parser = subparsers.add_parser("export", help="Export sessions to Markdown")
    export_parser.add_argument("--all", action="store_true", help="Export all sessions")
    export_parser.add_argument("--tool", help="Export only specific tool")
    export_parser.add_argument("--project", help="Export only specific project")

    # export-html command — single shareable standalone HTML file
    export_html_parser = subparsers.add_parser(
        "export-html", help="Export one session as a standalone HTML file"
    )
    export_html_parser.add_argument("session_id", help="Session ID (full or unique prefix)")
    export_html_parser.add_argument(
        "--output",
        help="Output file or directory (default: <session-id>.html in cwd)",
    )

    # search command
    search_parser = subparsers.add_parser("search", help="Search sessions")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--tool", help="Search only specific tool")
    search_parser.add_argument("--project", help="Search only specific project")

    # stats command
    subparsers.add_parser("stats", help="Show statistics")

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

    mem_list = memory_sub.add_parser("list", help="List recorded memories")
    mem_list.add_argument("--kind", help="filter by kind")
    mem_list.add_argument("--project", help="filter by project scope")
    mem_list.add_argument("--all", action="store_true", help="include superseded memories")
    mem_list.add_argument("--limit", type=int, default=50, help="max results")

    mem_search = memory_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query", help="search query")
    mem_search.add_argument("--kind", help="filter by kind")
    mem_search.add_argument("--project", help="filter by project scope")
    mem_search.add_argument("--limit", type=int, default=10, help="max results")
    mem_search.add_argument(
        "--semantic",
        action="store_true",
        help="rank by meaning (needs the 'semantic' extra; falls back to keywords)",
    )

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
    subparsers.add_parser("check", help="Check tool availability")

    # threads command
    subparsers.add_parser("threads", help="List threads")

    # rules command
    rules_parser = subparsers.add_parser("rules", help="Generate derived rules")
    rules_parser.add_argument("--limit", type=int, default=30, help="Max rules to extract")

    # prune command
    prune_parser = subparsers.add_parser("prune", help="Prune sessions by prompt count")
    prune_parser.add_argument(
        "--max-prompts",
        type=int,
        default=3,
        help="Delete sessions with this many prompts or fewer",
    )
    prune_parser.add_argument("--dry-run", action="store_true", help="Show what would be pruned")

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
        choices=["gemini", "ollama"],
        default="gemini",
        help="LLM provider to use (default: gemini)",
    )
    analyze_parser.add_argument("--model", help="Model name (e.g., gemini-2.0-flash, llama3.2)")
    analyze_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Generate basic stats without LLM",
    )
    analyze_parser.add_argument(
        "--output",
        help="Output file for stats (default: ~/.lore/stats/stats.json)",
    )

    # knowledge command - Build knowledge database
    knowledge_parser = subparsers.add_parser(
        "knowledge", help="Extract knowledge from sessions using LLM"
    )
    knowledge_parser.add_argument(
        "--provider",
        choices=["gemini", "ollama"],
        default="gemini",
        help="LLM provider to use (default: gemini)",
    )
    knowledge_parser.add_argument("--model", help="Model name (e.g., gemini-2.0-flash, llama3.2)")
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

    # format command - Format sessions with LLM
    format_parser = subparsers.add_parser(
        "format", help="Format sessions with AI-generated summaries and tags"
    )
    format_parser.add_argument(
        "--provider",
        choices=["gemini", "ollama"],
        default="gemini",
        help="LLM provider to use (default: gemini)",
    )
    format_parser.add_argument("--model", help="Model name (e.g., gemini-2.0-flash, llama3.2)")
    format_parser.add_argument("--session-id", help="Format specific session by ID")
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
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return
    handler(args)


if __name__ == "__main__":
    main()
