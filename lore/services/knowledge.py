"""Idempotent knowledge extraction shared by ``lore knowledge`` (#197).

Loads each session's real conversation from the v2 store, skips sessions whose
prompt input is unchanged, and persists a row only after a parsed provider
response, so a failed call is retried on the next run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..core.models import AUTOMATED_ORIGINS, Tool, UnifiedSession
from ..llm.tasks.knowledge import (
    FAILURE_PROVIDER_ERROR,
    FAILURE_PROVIDER_UNAVAILABLE,
    KnowledgeExtractor,
)
from ..services.index import load_deleted_session_ids
from ..storage.knowledge import get_session_knowledge, prompt_fingerprint, save_session_knowledge
from ..storage.reader import load_session_messages_v2
from ..utils.tooling import normalize_tool_name

# Bump when the extraction prompt changes so stored rows are re-extracted.
KNOWLEDGE_PROMPT_VERSION = 1


def sessions_for_extraction(
    output_dir: Path,
    *,
    limit: int,
    include_automated: bool,
    tool: Optional[str] = None,
    require_synced_messages: bool = False,
) -> List[UnifiedSession]:
    """Sessions eligible for knowledge extraction (same rules as ``lore knowledge``)."""
    index_file = output_dir / "index.json"
    if not index_file.exists():
        return []

    with open(index_file, encoding="utf-8") as handle:
        index = json.load(handle)

    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    hidden_ids = {
        str(entry.get("id"))
        for entry in index.get("sessions", [])
        if not include_automated and entry.get("origin") in AUTOMATED_ORIGINS
    }

    def _entry_to_session(entry: dict) -> UnifiedSession:
        created = entry.get("created")
        updated = entry.get("updated")
        from datetime import datetime

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
            session_tool = Tool(tool_raw)
        except ValueError:
            session_tool = Tool.CLAUDE_CODE
        return UnifiedSession(
            tool=session_tool,
            session_id=entry.get("id", ""),
            created_at=created_at,
            last_updated=updated_at,
            messages=[],
            project_path=entry.get("project"),
            thread_id=entry.get("thread_id"),
            title=entry.get("title"),
            origin=entry.get("origin") or "unknown",
        )

    sessions = [
        _entry_to_session(entry)
        for entry in index.get("sessions", [])
        if entry.get("id") not in deleted and entry.get("id") not in hidden_ids
    ]

    if tool:
        normalized = normalize_tool_name(tool) or tool
        sessions = [s for s in sessions if s.tool.value == normalized]

    sessions = sorted(sessions, key=lambda s: s.created_at, reverse=True)
    if require_synced_messages:
        kept: List[UnifiedSession] = []
        for session in sessions:
            if load_session_messages_v2(output_dir, session.session_id):
                kept.append(session)
            if len(kept) >= limit:
                break
        return kept
    return sessions[:limit]


@dataclass
class ExtractionReport:
    """Per-run counts of how each session was handled.

    ``failed`` is the total of the three cause buckets, kept as a property so
    the summary line and the exit-code message can never drift apart (#199).
    """

    extracted: int = 0
    unchanged: int = 0
    skipped_no_messages: int = 0
    redactions: int = 0
    provider_unavailable: int = 0
    provider_error: int = 0
    unparsable: int = 0

    @property
    def failed(self) -> int:
        """Every extraction that produced no entry, whatever the cause."""
        return self.provider_unavailable + self.provider_error + self.unparsable

    def count_failure(self, reason: Optional[str]) -> None:
        """Count one failed extraction under its cause.

        An unknown or missing reason counts as ``unparsable`` — "the provider
        did not answer" would blame the wrong side.
        """
        if reason == FAILURE_PROVIDER_UNAVAILABLE:
            self.provider_unavailable += 1
        elif reason == FAILURE_PROVIDER_ERROR:
            self.provider_error += 1
        else:
            self.unparsable += 1

    def failure_breakdown(self) -> str:
        """Non-zero causes, e.g. ``'1 provider unavailable, 2 unparsable'``."""
        parts = []
        if self.provider_unavailable:
            parts.append(f"{self.provider_unavailable} provider unavailable")
        if self.provider_error:
            parts.append(f"{self.provider_error} provider error")
        if self.unparsable:
            parts.append(f"{self.unparsable} unparsable")
        return ", ".join(parts)


def run_extraction(
    output_dir: Path,
    sessions: List[UnifiedSession],
    extractor: KnowledgeExtractor,
    model: str,
) -> ExtractionReport:
    """Extract knowledge for ``sessions`` whose prompt input changed.

    ``sessions`` may carry no messages (index entries); the conversation is
    loaded from the v2 store under ``output_dir``. Sessions without synced
    messages are never sent to the provider.
    """
    report = ExtractionReport()
    for session in sessions:
        messages = load_session_messages_v2(output_dir, session.session_id)
        if not messages:
            report.skipped_no_messages += 1
            continue
        session.messages = messages

        # The whole prompt (tool, project, date, conversation) is the input a stored
        # result depends on, so a change to any of them re-extracts.
        fingerprint = prompt_fingerprint(
            extractor.build_prompt(session), model, KNOWLEDGE_PROMPT_VERSION
        )
        stored = get_session_knowledge(output_dir, session.session_id)
        if stored is not None and stored["fingerprint"] == fingerprint:
            report.unchanged += 1
            continue

        entry, failure = extractor.extract_with_reason(session)
        if entry is None:
            report.count_failure(failure)
            continue
        report.redactions += entry.redactions
        save_session_knowledge(
            output_dir,
            session.session_id,
            fingerprint,
            model,
            KNOWLEDGE_PROMPT_VERSION,
            entry.to_dict(),
        )
        report.extracted += 1
    return report
