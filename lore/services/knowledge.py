"""Idempotent knowledge extraction shared by ``lore knowledge`` (#197).

Loads each session's real conversation from the v2 store, skips sessions whose
prompt input is unchanged, and persists a row only after a parsed provider
response, so a failed call is retried on the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..core.models import UnifiedSession
from ..llm.tasks.knowledge import KnowledgeExtractor
from ..storage.knowledge import get_session_knowledge, prompt_fingerprint, save_session_knowledge
from ..storage.reader import load_session_messages_v2

# Bump when the extraction prompt changes so stored rows are re-extracted.
KNOWLEDGE_PROMPT_VERSION = 1


@dataclass
class ExtractionReport:
    """Per-run counts of how each session was handled."""

    extracted: int = 0
    unchanged: int = 0
    skipped_no_messages: int = 0
    failed: int = 0


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

        entry = extractor.extract_from_session(session)
        if entry is None:
            report.failed += 1
            continue
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
