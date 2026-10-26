"""Ordered ``lore improve`` pipeline: embeddings, extraction, memory candidates (#197)."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

from ..llm import CloudOptInRequired, LLMConfig, get_provider
from ..llm.tasks.knowledge import KnowledgeExtractor
from ..services.index import load_deleted_session_ids
from ..services.knowledge import run_extraction, sessions_for_extraction
from ..services.summarize import is_summary_stale
from ..services.summarize_cli import default_model
from ..storage.embeddings import (
    cosine_similarity,
    embed_text,
    embeddings_available,
    unpack_vector,
)
from ..storage.knowledge import list_session_knowledge
from ..storage.memory import (
    MAX_BODY_LEN,
    MAX_TITLE_LEN,
    _connect,
    add_memory,
    embed_missing_memories,
    similarity_threshold,
)
from ..storage.summaries import list_summaries
from ..utils.security import validate_search_param

STAGE_NAMES = (
    "embed_memories",
    "extract_knowledge",
    "distill_lessons",
    "from_summaries",
)

_PER_SESSION_CAP = 5
_PER_RUN_CAP = 50
_OUTCOME_NOTE_MAX = 400

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_EDGE_PUNCT_RE = re.compile(r'^[\s.,;:!?"\'\-]+|[\s.,;:!?"\'\-]+$')


@dataclass
class StageResult:
    stage: str
    status: str
    reason: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "reason": self.reason,
            "counts": dict(self.counts),
            "duration_ms": self.duration_ms,
        }


def normalize_scope_project(project: str | None) -> str | None:
    """Match ``memory_write`` / CLI project scope handling."""
    if not project:
        return None
    cleaned = str(project).strip()
    if not cleaned or not validate_search_param(cleaned):
        return None
    return cleaned


def _normalize_content(text: str) -> str:
    lowered = text.lower()
    collapsed = _WS_RE.sub(" ", lowered).strip()
    return _EDGE_PUNCT_RE.sub("", collapsed)


def _clean_title(topic: str, body: str) -> str:
    title = _CTRL_RE.sub("", (topic or "").strip())
    if not title:
        title = _CTRL_RE.sub("", (body or "").strip()[:80])
    return title[:MAX_TITLE_LEN]


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _wrap_stage(name: str, fn: Callable[[], StageResult]) -> StageResult:
    start = time.monotonic()
    try:
        result = fn()
        result.duration_ms = result.duration_ms or _elapsed_ms(start)
        return result
    except CloudOptInRequired:
        # Missing cloud opt-in is a configuration refusal, not a stage failure:
        # it must reach the caller instead of becoming a per-stage "errored" row.
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:200]
        return StageResult(
            stage=name, status="errored", reason=reason, duration_ms=_elapsed_ms(start)
        )


def _existing_content_hashes(output_dir: Path) -> set[str]:
    # Every row counts, whatever its status: a rejected or superseded lesson must not return.
    conn = _connect(output_dir)
    try:
        rows = conn.execute("SELECT body FROM memory").fetchall()
    finally:
        conn.close()
    return {_normalize_content(row["body"]) for row in rows if row["body"]}


def _is_semantically_known(output_dir: Path, scope_project: str | None, text: str) -> bool:
    """True when ``text`` nearly duplicates a live or rejected memory in scope."""
    if not embeddings_available():
        return False
    vector = embed_text(text)
    if vector is None:
        return False
    threshold = similarity_threshold()
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.title, e.vector
            FROM memory m
            JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE m.superseded_by IS NULL
              AND m.status IN ('confirmed', 'rejected', 'candidate')
              AND m.scope_project IS ?
            """,
            (scope_project,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        sim = cosine_similarity(vector, unpack_vector(row["vector"]))
        if sim >= threshold:
            return True
    return False


@dataclass
class _PromoteState:
    hashes: set[str]
    per_session: Dict[str, int]
    run_added: int = 0
    counts: Dict[str, int] = field(
        default_factory=lambda: {
            "added": 0,
            "duplicate": 0,
            "invalid": 0,
            "already_known": 0,
            "capped": 0,
        }
    )

    def try_add(
        self,
        output_dir: Path,
        *,
        session_id: str,
        kind: str,
        title: str,
        body: str,
        scope_project: str | None,
        dry_run: bool,
    ) -> None:
        body = (body or "").strip()
        title = _clean_title(title, body)
        if not body or not title:
            self.counts["invalid"] += 1
            return
        if len(title) > MAX_TITLE_LEN or len(body) > MAX_BODY_LEN:
            self.counts["invalid"] += 1
            return
        norm = _normalize_content(body)
        if not norm:
            self.counts["invalid"] += 1
            return
        if norm in self.hashes:
            self.counts["duplicate"] += 1
            return
        if _is_semantically_known(output_dir, scope_project, f"{title}\n{body}"):
            self.counts["already_known"] += 1
            return
        if self.per_session.get(session_id, 0) >= _PER_SESSION_CAP:
            self.counts["capped"] += 1
            return
        if self.run_added >= _PER_RUN_CAP:
            self.counts["capped"] += 1
            return
        if dry_run:
            self.hashes.add(norm)
            self.per_session[session_id] = self.per_session.get(session_id, 0) + 1
            self.run_added += 1
            self.counts["added"] += 1
            return
        add_memory(
            output_dir,
            kind,
            title,
            body,
            author="lore-improve",
            scope_project=scope_project,
            source_session=session_id,
            status="candidate",
        )
        self.hashes.add(norm)
        self.per_session[session_id] = self.per_session.get(session_id, 0) + 1
        self.run_added += 1
        self.counts["added"] += 1


def _session_projects(output_dir: Path) -> Dict[str, str | None]:
    index_file = output_dir / "index.json"
    if not index_file.exists():
        return {}
    with open(index_file, encoding="utf-8") as handle:
        index = json.load(handle)
    return {
        str(entry.get("id")): normalize_scope_project(entry.get("project"))
        for entry in index.get("sessions", [])
        if entry.get("id")
    }


def _distill_knowledge(
    output_dir: Path,
    *,
    dry_run: bool,
) -> StageResult:
    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    rows = list_session_knowledge(output_dir)
    if not rows:
        return StageResult("distill_lessons", "skipped", reason="no_extracted_knowledge")

    projects = _session_projects(output_dir)
    state = _PromoteState(hashes=_existing_content_hashes(output_dir), per_session={})
    for row in rows:
        session_id = row["session_id"]
        if session_id in deleted:
            continue
        payload = row.get("payload") or {}
        topic = str(payload.get("topic") or "")
        scope = projects.get(session_id)
        for decision in payload.get("decisions") or []:
            if isinstance(decision, str) and decision.strip():
                state.try_add(
                    output_dir,
                    session_id=session_id,
                    kind="decision",
                    title=topic,
                    body=decision.strip(),
                    scope_project=scope,
                    dry_run=dry_run,
                )
        for lesson in payload.get("key_points") or []:
            if isinstance(lesson, str) and lesson.strip():
                state.try_add(
                    output_dir,
                    session_id=session_id,
                    kind="lesson",
                    title=topic,
                    body=lesson.strip(),
                    scope_project=scope,
                    dry_run=dry_run,
                )

    status = "completed"
    if state.counts["added"] == 0 and not any(
        state.counts[k] for k in ("duplicate", "invalid", "already_known", "capped")
    ):
        status = "already_completed"
    elif state.counts["added"] == 0:
        status = "already_completed"
    return StageResult("distill_lessons", status, counts=state.counts)


def _iter_summary_items(payload: dict[str, Any]) -> Iterable[Tuple[str, str, str]]:
    """Yield (kind, title, body) tuples from a stored session summary payload."""
    topic = str(payload.get("one_liner") or payload.get("topic") or "")
    for item in payload.get("decisions") or []:
        text = item.get("text") if isinstance(item, dict) else str(item)
        if isinstance(text, str) and text.strip():
            yield ("decision", topic, text.strip())

    outcome = payload.get("outcome")
    outcome_text = ""
    if isinstance(outcome, dict):
        outcome_text = str(outcome.get("text") or "").strip()

    raw_artifacts = payload.get("artifacts")
    artifacts: Dict[str, Any] = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    errors = artifacts.get("errors") or payload.get("errors") or []
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                err_text = str(err.get("text") or "").strip()
                resolution = str(err.get("resolution") or "").strip()
            else:
                err_text = str(err).strip()
                resolution = ""
            if not err_text:
                continue
            if not resolution:
                continue
            body = f"Error: {err_text}. Resolution: {resolution}"
            yield ("lesson", topic or err_text[:80], body)

    if (
        outcome_text
        and len(outcome_text) <= _OUTCOME_NOTE_MAX
        and re.search(r"[.!?]\s*$", outcome_text)
    ):
        yield ("note", topic or outcome_text[:80], outcome_text)


def _from_summaries(output_dir: Path, *, dry_run: bool) -> StageResult:
    summaries = list_summaries(output_dir, level="session")
    visible: List[dict[str, Any]] = []
    for row in summaries:
        session_id = row.get("session_id")
        if not session_id:
            continue
        stale = is_summary_stale(output_dir, session_id)
        if stale is True:
            continue
        payload = row.get("payload")
        if isinstance(payload, dict):
            visible.append({"session_id": session_id, "payload": payload})
    if not visible:
        return StageResult("from_summaries", "skipped", reason="no_summaries")

    deleted = load_deleted_session_ids(output_dir / "deleted_sessions.json")
    projects = _session_projects(output_dir)
    state = _PromoteState(hashes=_existing_content_hashes(output_dir), per_session={})
    sessions_seen = 0
    for row in visible:
        session_id = row["session_id"]
        if session_id in deleted:
            continue
        sessions_seen += 1
        for kind, title, body in _iter_summary_items(row["payload"]):
            state.try_add(
                output_dir,
                session_id=session_id,
                kind=kind,
                title=title,
                body=body,
                scope_project=projects.get(session_id),
                dry_run=dry_run,
            )
    counts = dict(state.counts)
    counts["sessions"] = sessions_seen
    status = "completed"
    if state.counts["added"] == 0:
        status = "already_completed"
    return StageResult("from_summaries", status, counts=counts)


def _stage_embed(output_dir: Path, *, dry_run: bool) -> StageResult:
    if dry_run:
        return StageResult("embed_memories", "skipped", reason="dry_run")
    if not embeddings_available():
        return StageResult("embed_memories", "skipped", reason="no_embedding_backend")
    conn = _connect(output_dir)
    try:
        missing = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM memory m
            LEFT JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE e.memory_id IS NULL AND m.status != 'rejected'
            """
        ).fetchone()["n"]
    finally:
        conn.close()
    if not missing:
        return StageResult("embed_memories", "already_completed")
    embedded = embed_missing_memories(output_dir)
    return StageResult(
        "embed_memories",
        "completed",
        counts={"embedded": embedded, "missing": int(missing)},
    )


def _stage_extract(
    output_dir: Path,
    *,
    provider_name: str | None,
    model: str | None,
    limit: int,
    include_automated: bool,
    dry_run: bool,
    allow_cloud: bool = False,
) -> StageResult:
    if dry_run:
        return StageResult("extract_knowledge", "skipped", reason="dry_run")
    chosen = (provider_name or os.environ.get("LORE_LLM_PROVIDER") or "").strip()
    if not chosen:
        return StageResult("extract_knowledge", "skipped", reason="no_llm")
    try:
        config = LLMConfig.for_transcripts(provider=chosen, model=model or default_model(chosen))
        provider = get_provider(
            provider=config.provider,
            model=config.model,
            cache_enabled=config.cache_enabled,
            options=config.options,
            num_ctx=config.num_ctx,
            allow_cloud=allow_cloud,
        )
    except CloudOptInRequired:
        # Opting in is a deliberate user decision: report it, never silently skip it.
        raise
    except Exception:
        return StageResult("extract_knowledge", "skipped", reason="provider_unavailable")

    if not provider.is_available():
        return StageResult("extract_knowledge", "skipped", reason="provider_unavailable")

    sessions = sessions_for_extraction(
        output_dir,
        limit=limit,
        include_automated=include_automated,
        require_synced_messages=True,
    )
    label = f"{config.provider}/{config.model}"
    extractor = KnowledgeExtractor(provider, output_dir / "knowledge")
    report = run_extraction(output_dir, sessions, extractor, label)
    counts = {
        "extracted": report.extracted,
        "unchanged": report.unchanged,
        "skipped_no_messages": report.skipped_no_messages,
        "failed": report.failed,
        "redactions": report.redactions,
    }
    return StageResult("extract_knowledge", "completed", reason=label, counts=counts)


def run_improve(
    output_dir: Path,
    *,
    provider_name: str | None,
    model: str | None,
    limit: int,
    stages: Sequence[str] | None = None,
    dry_run: bool = False,
    include_automated: bool = False,
    allow_cloud: bool = False,
) -> List[StageResult]:
    """Run improve stages in order; failures in one stage do not stop later ones.

    A non-local provider without opt-in raises :class:`CloudOptInRequired` before any
    provider is constructed.
    """
    output_dir = output_dir.expanduser()
    selected = tuple(stages) if stages else STAGE_NAMES
    results: List[StageResult] = []

    runners: Dict[str, Callable[[], StageResult]] = {
        "embed_memories": lambda: _stage_embed(output_dir, dry_run=dry_run),
        "extract_knowledge": lambda: _stage_extract(
            output_dir,
            provider_name=provider_name,
            model=model,
            limit=limit,
            include_automated=include_automated,
            dry_run=dry_run,
            allow_cloud=allow_cloud,
        ),
        "distill_lessons": lambda: _distill_knowledge(output_dir, dry_run=dry_run),
        "from_summaries": lambda: _from_summaries(output_dir, dry_run=dry_run),
    }

    for name in STAGE_NAMES:
        if name not in selected:
            continue
        fn = runners.get(name)
        if fn is None:
            continue
        results.append(_wrap_stage(name, fn))
    return results
