"""Shared extractor-iteration and index-building logic.

This module holds the "iterate available extractors and collect
sessions" routine that used to be copy-pasted into ``web_data`` (twice),
``mcp`` and the CLI. It is framework-free: no Flask, no interface
imports.

Both the web interface and the MCP server build on these helpers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

from lore.core.models import UnifiedSession
from lore.exporters.index import IndexBuilder, _stat_mtime_ns
from lore.extractors.base import BaseExtractor
from lore.extractors.factory import get_all_extractors
from lore.titles.generator import TitleGenerator, TitleStrategy

logger = logging.getLogger(__name__)


class ActionJobCancelledError(RuntimeError):
    """Raised when a long-running build/extraction is cancelled by the user.

    Defined in the (framework-free) service layer so both ``services`` and
    the Flask-coupled interface modules can reference the same exception
    type. ``lore.interfaces.web_utils`` re-exports it for backwards
    compatibility.
    """


def select_extractors(
    tool_filter: Optional[str] = None,
    extractors: Optional[Iterable[BaseExtractor]] = None,
) -> list[BaseExtractor]:
    """Return the available extractors, optionally filtered to one tool."""
    candidates = list(extractors) if extractors is not None else get_all_extractors()
    return [
        extractor
        for extractor in candidates
        if extractor.is_available() and (not tool_filter or extractor.tool.value == tool_filter)
    ]


def collect_sessions(
    tool_filter: Optional[str] = None,
    *,
    apply_titles: bool = True,
    deleted_ids: Optional[set[str]] = None,
) -> list[UnifiedSession]:
    """Iterate available extractors and collect their sessions.

    This is the shared core of the old ``load_sessions_for_tool`` and the
    MCP server's ``build_index_if_missing`` loop.

    Args:
        tool_filter: When set, only the matching tool's extractor runs.
        apply_titles: When True, a fast ``TitleGenerator`` annotates each
            session's title in place.
        deleted_ids: Session ids to drop from the result (tombstones).

    Extractor failures are logged and skipped — one broken tool never
    aborts the whole collection.
    """
    title_generator = TitleGenerator(strategy=TitleStrategy.FAST) if apply_titles else None
    sessions: list[UnifiedSession] = []

    for extractor in select_extractors(tool_filter):
        try:
            extracted = list(extractor.extract_sessions())
        except Exception as exc:
            logger.debug(
                "Extractor %s failed during session collection: %s",
                extractor.tool.value,
                exc,
            )
            continue
        if title_generator is not None:
            for session in extracted:
                title = title_generator.generate(session, force=False)
                if title:
                    session.title = title
        sessions.extend(extracted)

    if deleted_ids:
        sessions = [s for s in sessions if s.session_id not in deleted_ids]
    return sessions


def find_live_session(
    session_id: str,
    tool_filter: Optional[str] = None,
    *,
    apply_titles: bool = True,
    deleted_ids: Optional[set[str]] = None,
) -> Optional[UnifiedSession]:
    """Locate one live session by id, preferring a direct file lookup (#127).

    Cold-cache bulk collection parses every session file of a tool just to
    serve a single-session lookup. Extractors with deterministic layouts
    (see ``BaseExtractor.find_session_by_id``) resolve the session by
    parsing only its source file; tools without such a layout return
    ``None`` here and the caller falls back to its cached bulk path.
    Tombstoned ids are never returned.

    Extractor failures are logged and skipped — one broken tool never
    aborts the lookup.
    """
    if not session_id:
        return None
    if deleted_ids and session_id in deleted_ids:
        return None
    title_generator = TitleGenerator(strategy=TitleStrategy.FAST) if apply_titles else None
    for extractor in select_extractors(tool_filter):
        try:
            session = extractor.find_session_by_id(session_id)
        except Exception as exc:
            logger.debug(
                "Extractor %s failed direct lookup for %s: %s",
                extractor.tool.value,
                session_id,
                exc,
            )
            continue
        if session is None or session.session_id != session_id:
            continue
        if title_generator is not None:
            title = title_generator.generate(session, force=False)
            if title:
                session.title = title
        return session
    return None


def build_index_if_missing(
    output_dir: Path,
    index_path: Path,
) -> None:
    """Build a minimal index from extractors if ``index_path`` is absent.

    Used by the MCP server, which does not need the web app's incremental
    rebuild logic — a one-shot full extraction is sufficient.
    """
    if index_path.exists():
        return
    sessions: list[UnifiedSession] = []
    for extractor in get_all_extractors():
        if not extractor.is_available():
            continue
        try:
            sessions.extend(list(extractor.extract_sessions()))
        except Exception as exc:
            logger.debug(
                "Extractor %s failed during build_index_if_missing: %s",
                extractor.tool.value,
                exc,
            )
            continue
    IndexBuilder(output_dir).build_index(sessions, {})


def build_search_index(
    output_dir: Path,
    index_path: Path,
    *,
    deleted_ids: Optional[set[str]] = None,
    tool_filter: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    incremental: bool = True,
) -> list[dict]:
    """Build the full search index from extractors.

    When ``incremental=True`` (default) sessions already present in the
    existing index whose source-file mtime hasn't changed are reused
    verbatim instead of being re-processed by :class:`IndexBuilder`. Set
    ``incremental=False`` to force a full rebuild.

    Returns a list of report dicts:
    - ``{"extractor": ..., "error": ...}`` for an extractor that crashed.
    - ``{"extractor": ..., "skipped": {reason: n}, "imported": k}`` for an
      extractor whose quality filter dropped sessions (#1e — no silent drops).
    Consumers distinguish the two by presence of the ``"error"`` key.
    """
    selected = select_extractors(tool_filter)

    existing_by_id: dict[str, dict] = {}
    if incremental and index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as handle:
                existing_payload = json.load(handle)
            for entry in existing_payload.get("sessions", []) or []:
                sid = str(entry.get("id") or "")
                if sid:
                    existing_by_id[sid] = entry
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read existing index for incremental build: %s", exc)

    reused_entries: list[dict] = []
    # Ids of the reused (unchanged) sessions. They are yielded through the SAME
    # stream as refreshed sessions; build_index routes an id in this set to a
    # v2-only write (its JSON/legacy entry comes from reused_entries). This is
    # what lets warm incremental sync avoid holding every unchanged session in a
    # list to re-write its v2 message rows (#96/#103/#35).
    reused_ids: set[str] = set()
    errors: list[dict] = []
    title_generator = TitleGenerator(strategy=TitleStrategy.FAST)
    total = len(selected) or 1
    deleted = deleted_ids or set()
    refresh_count = 0

    import time as _time

    def session_stream():
        """Yield every session to index, one at a time (#96/#103).

        Runs the extractor walk lazily so ``build_index`` streams straight from
        the extractors — no full session list is ever held. Both refreshed and
        reused sessions are yielded (reused ones tagged via ``reused_ids``);
        ``reused_entries`` (light prior dicts) and per-extractor skip/error
        reports (``errors``) are collected as side effects. build_index consumes
        this generator to exhaustion before finalizing, so those side-effect
        lists are complete by the time it needs them.
        """
        nonlocal refresh_count
        for i, extractor in enumerate(selected, start=1):
            if should_stop and should_stop():
                raise ActionJobCancelledError("Cancelled by user")
            # Base progress for this tool (covers the range [base, base+per_tool))
            # so we can move the bar smoothly as sessions stream in. Without these
            # mid-extractor updates the bar visibly froze for the user — claude-code
            # alone needs to walk 368+ JSONL files with no other I/O signal.
            base_progress = 15 + int((i - 1) / total * 45)
            per_tool = max(1, int(45 / total))
            tool_name = extractor.tool.value
            if progress_callback:
                progress_callback(base_progress, f"Loading {tool_name}")
            imported = 0
            try:
                sess_count = 0
                last_tick = _time.monotonic()
                for session in extractor.extract_sessions():
                    if should_stop and should_stop():
                        raise ActionJobCancelledError("Cancelled by user")
                    sess_count += 1
                    now = _time.monotonic()
                    if progress_callback and now - last_tick >= 1.0:
                        # Crawl the bar within this tool's slice; the count keeps
                        # the user looking at a moving number even if the bar
                        # nudges only a percent.
                        sub = min(per_tool - 1, int(sess_count / 50))
                        progress_callback(
                            base_progress + sub,
                            f"Loading {tool_name} ({sess_count} sessions)",
                        )
                        last_tick = now

                    if session.session_id in deleted:
                        continue

                    if incremental:
                        prior = existing_by_id.get(session.session_id)
                        if prior is not None:
                            prior_mtime = prior.get("source_mtime")
                            current_mtime = _stat_mtime_ns(session.source_path)
                            if (
                                prior_mtime is not None
                                and current_mtime is not None
                                and prior_mtime == current_mtime
                                and session.session_id not in reused_ids
                            ):
                                # Unchanged: its JSON/legacy entry is reused
                                # verbatim from the prior dict; yield the full
                                # session so build_index writes its v2 message
                                # rows (#35), then drops it — no list retained.
                                reused_entries.append(prior)
                                reused_ids.add(session.session_id)
                                yield session
                                continue

                    title = title_generator.generate(session, force=False)
                    if title:
                        session.title = title
                    imported += 1
                    refresh_count += 1
                    yield session
            except ActionJobCancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Extractor %s failed during index build: %s",
                    extractor.tool.value,
                    exc,
                    exc_info=True,
                )
                errors.append({"extractor": extractor.tool.value, "error": str(exc)})
                continue

            # Surface how many sessions the quality filter dropped instead of
            # letting them vanish silently inside the generator (#1e).
            skipped = dict(getattr(extractor, "skip_counts", {}) or {})
            if skipped:
                total_skipped = sum(skipped.values())
                logger.info(
                    "Extractor %s: imported %d, skipped %d (%s)",
                    tool_name,
                    imported,
                    total_skipped,
                    ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())),
                )
                errors.append({"extractor": tool_name, "skipped": skipped, "imported": imported})

    # reused_entries / reused_ids are populated as session_stream runs. They are
    # consumed only after the generator drains (build_index seeds reused-entry
    # rows at finalize), so both are complete by then. Passing the live objects
    # lets build_index read them post-drain without materialising sessions here.
    IndexBuilder(output_dir).build_index(
        session_stream(),
        {},
        reused_entries=reused_entries,
        reused_ids=reused_ids,
    )

    if progress_callback:
        message = (
            f"Index written ({len(reused_entries)} reused, {refresh_count} refreshed)"
            if incremental
            else "Index written"
        )
        progress_callback(62, message)

    return errors
