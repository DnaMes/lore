"""Evidence-backed workflow metrics (#188).

Every result belongs to exactly one class and the classes are never mixed:

``observed``
    A value read straight off a documented source field: a tool-event status, a
    session ``origin``. Nothing is derived.
``computed``
    Derived deterministically from observed fields (a mean, a median,
    a ratio). Reproducible from the same archive; no judgement involved.
``inferred``
    A heuristic judgement about an outcome. Produced only when explicitly
    requested, always labelled, and never added into an observed number
    (:meth:`MetricResult.__add__` refuses to mix kinds).

Each result also carries the source fields it reads, how many sessions those
fields were actually present on (``covered_sessions`` out of
``total_sessions``) and the caveats that limit the number. When a field is
absent the value is ``None`` and coverage is ``0`` with a caveat saying so — a
missing field never becomes a confident zero.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from lore.core.models import ORIGINS, Role, UnifiedSession
from lore.utils.datetime import make_naive

OBSERVED = "observed"
COMPUTED = "computed"
INFERRED = "inferred"
METRIC_KINDS = (OBSERVED, COMPUTED, INFERRED)

# Tool-event classification. Only these spellings are trusted; anything else is
# counted as unclassified rather than assumed successful.
FAILED_TOOL_STATUSES = frozenset({"error", "failed", "failure", "fail"})
OK_TOOL_STATUSES = frozenset({"success", "succeeded", "ok", "completed", "complete", "passed"})

# Outcome inference markers, matched case-insensitively as whole words. A
# message carrying both kinds is "unclassified" (ambiguous), never guessed.
SUCCESS_MARKERS = ("done", "complete", "completed", "fixed", "passing", "resolved", "implemented")
FAILURE_MARKERS = ("error", "failed", "cannot", "unable", "blocked", "not working")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class MetricResult:
    """One metric value with its class, provenance and coverage.

    ``value`` is ``None`` when the source field was absent; callers must check
    :attr:`coverage` before reporting a number at all.
    """

    name: str
    value: Any
    kind: str
    source_fields: Tuple[str, ...]
    covered_sessions: int
    total_sessions: int
    caveats: Tuple[str, ...] = ()

    @property
    def coverage(self) -> float:
        """Fraction of sessions the source field was present on (0.0 when none)."""
        if self.total_sessions <= 0:
            return 0.0
        return round(self.covered_sessions / self.total_sessions, 4)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind,
            "source_fields": list(self.source_fields),
            "covered_sessions": self.covered_sessions,
            "total_sessions": self.total_sessions,
            "coverage": self.coverage,
            "caveats": list(self.caveats),
        }

    def __add__(self, other: object) -> "MetricResult":
        """Sum two results of the same class — and refuse to mix classes.

        Adding an ``inferred`` result to an ``observed``/``computed`` one is a
        category error (a guess would become part of a measurement), so it
        raises. Same-class sums are allowed for counts only.
        """
        if not isinstance(other, MetricResult):
            return NotImplemented
        if self.kind != other.kind:
            raise ValueError(
                f"cannot combine {self.kind} metric {self.name!r} with "
                f"{other.kind} metric {other.name!r}: inferred results are never "
                f"summed with observed or computed ones"
            )
        if not (_is_number(self.value) and _is_number(other.value)):
            raise TypeError(f"metrics {self.name!r} and {other.name!r} are not numeric")
        return MetricResult(
            name=f"{self.name}+{other.name}",
            value=self.value + other.value,
            kind=self.kind,
            source_fields=tuple(sorted(set(self.source_fields) | set(other.source_fields))),
            covered_sessions=min(self.covered_sessions, other.covered_sessions),
            total_sessions=self.total_sessions,
            caveats=tuple(dict.fromkeys(self.caveats + other.caveats)),
        )


def _absent(
    name: str,
    kind: str,
    source_fields: Tuple[str, ...],
    total_sessions: int,
    reason: str,
) -> MetricResult:
    """Result for a metric whose source field was not present on any session."""
    return MetricResult(
        name=name,
        value=None,
        kind=kind,
        source_fields=source_fields,
        covered_sessions=0,
        total_sessions=total_sessions,
        caveats=(f"no data: {reason}",),
    )


def sessions_by_origin(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Sessions per ``origin`` (``observed``): interactive / exec / cron / subagent."""
    source = ("UnifiedSession.origin",)
    total = len(sessions)
    counts: Dict[str, int] = {}
    for session in sessions:
        origin = getattr(session, "origin", None)
        if origin in ORIGINS and origin != "unknown":
            counts[origin] = counts.get(origin, 0) + 1
    covered = sum(counts.values())
    if covered == 0:
        return _absent(
            "sessions_by_origin",
            OBSERVED,
            source,
            total,
            f"all {total} session(s) store origin='unknown' (the extractor did not "
            "record one); an unknown origin is not interactive",
        )
    caveats: Tuple[str, ...] = ()
    unknown = total - covered
    if unknown:
        caveats = (
            f"{unknown} session(s) store origin='unknown' and are excluded from the "
            "breakdown rather than counted as interactive",
        )
    return MetricResult(
        name="sessions_by_origin",
        value={origin: counts[origin] for origin in sorted(counts)},
        kind=OBSERVED,
        source_fields=source,
        covered_sessions=covered,
        total_sessions=total,
        caveats=caveats,
    )


def _mean_over_messages(
    name: str,
    sessions: Sequence[UnifiedSession],
    per_session: Callable[[UnifiedSession], int],
    source_fields: Tuple[str, ...],
    note: str,
) -> MetricResult:
    """Shared shape for the per-session means; sessions without messages are excluded."""
    total = len(sessions)
    loaded = [s for s in sessions if s.messages]
    if not loaded:
        return _absent(
            name,
            COMPUTED,
            source_fields,
            total,
            f"no session has stored messages ({total} session(s) seen)",
        )
    values = [per_session(session) for session in loaded]
    caveats = [note]
    missing = total - len(loaded)
    if missing:
        caveats.append(f"{missing} session(s) have no stored messages and are excluded")
    return MetricResult(
        name=name,
        value=round(sum(values) / len(values), 4),
        kind=COMPUTED,
        source_fields=source_fields,
        covered_sessions=len(loaded),
        total_sessions=total,
        caveats=tuple(caveats),
    )


def prompts_per_session(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Mean user prompts per session (``computed``)."""
    return _mean_over_messages(
        "prompts_per_session",
        sessions,
        lambda session: session.user_prompt_count,
        ("UnifiedMessage.role", "UnifiedMessage.kind", "UnifiedMessage.content"),
        "a prompt is a user message with kind='text' and non-empty content; "
        "the value is the arithmetic mean over covered sessions",
    )


def messages_per_session(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Mean stored messages per session (``computed``)."""
    return _mean_over_messages(
        "messages_per_session",
        sessions,
        lambda session: len(session.messages),
        ("UnifiedSession.messages",),
        "counts every stored message row, including injected context and "
        "compaction summaries; the value is the arithmetic mean",
    )


def _classify_tool_event(event: Any) -> Optional[bool]:
    """True = failed, False = succeeded, None = not classifiable."""
    if not isinstance(event, dict):
        return None
    if event.get("is_error") is True:
        return True
    status = event.get("status")
    if not isinstance(status, str):
        return False if event.get("is_error") is False else None
    normalised = status.strip().lower()
    if normalised in FAILED_TOOL_STATUSES:
        return True
    if normalised in OK_TOOL_STATUSES:
        return False
    return None


def tool_call_failure_rate(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Share of classifiable tool events that failed (``observed``).

    Reads the compact ``tool_events`` rows the extractors record
    (``name``/``status``/``is_error``); arguments and results are never stored
    there, so this is a status count, not a taxonomy of failures.
    """
    source = ("UnifiedMessage.tool_events[].status", "UnifiedMessage.tool_events[].is_error")
    total = len(sessions)
    failed = 0
    classified = 0
    unclassified = 0
    covered = 0
    for session in sessions:
        session_has_event = False
        for message in session.messages:
            for event in getattr(message, "tool_events", None) or []:
                outcome = _classify_tool_event(event)
                if outcome is None:
                    unclassified += 1
                    continue
                classified += 1
                session_has_event = True
                if outcome:
                    failed += 1
        if session_has_event:
            covered += 1
    if classified == 0:
        return _absent(
            "tool_call_failure_rate",
            OBSERVED,
            source,
            total,
            f"no stored tool event carries a recognised status or is_error "
            f"({unclassified} event(s) seen)",
        )
    caveats = [f"{failed} of {classified} classified tool event(s) failed"]
    if unclassified:
        caveats.append(
            f"{unclassified} tool event(s) had no recognised status and are excluded, "
            "not assumed successful"
        )
    return MetricResult(
        name="tool_call_failure_rate",
        value=round(failed / classified, 4),
        kind=OBSERVED,
        source_fields=source,
        covered_sessions=covered,
        total_sessions=total,
        caveats=tuple(caveats),
    )


def median_session_duration_seconds(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Median wall-clock session length in seconds (``computed``).

    ``created_at`` and ``last_updated`` are compared after dropping ``tzinfo``
    (the archive's storage frame); sessions where the two contradict each other
    are excluded rather than clamped.
    """
    source = ("UnifiedSession.created_at", "UnifiedSession.last_updated")
    total = len(sessions)
    durations: List[float] = []
    contradictory = 0
    for session in sessions:
        created = getattr(session, "created_at", None)
        updated = getattr(session, "last_updated", None)
        if created is None or updated is None:
            contradictory += 1
            continue
        try:
            delta = make_naive(updated) - make_naive(created)
        except TypeError:  # mixed datetime-like values; not usable
            contradictory += 1
            continue
        if delta.total_seconds() < 0:
            contradictory += 1
            continue
        durations.append(delta.total_seconds())
    if not durations:
        return _absent(
            "median_session_duration_seconds",
            COMPUTED,
            source,
            total,
            f"no session has a usable created_at <= last_updated pair ({contradictory} excluded)",
        )
    caveats = [
        "wall-clock span from the first to the last stored message; it includes "
        "idle time between turns"
    ]
    if contradictory:
        caveats.append(
            f"{contradictory} session(s) had a missing or inverted "
            "created_at/last_updated pair and are excluded"
        )
    return MetricResult(
        name="median_session_duration_seconds",
        value=round(float(statistics.median(durations)), 4),
        kind=COMPUTED,
        source_fields=source,
        covered_sessions=len(durations),
        total_sessions=total,
        caveats=tuple(caveats),
    )


def _last_assistant_text(session: UnifiedSession) -> Optional[str]:
    for message in reversed(session.messages):
        if message.role == Role.ASSISTANT and message.kind == "text":
            content = (message.content or "").strip()
            if content:
                return content
    return None


def _marker_hit(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    for marker in markers:
        start = 0
        while True:
            index = lowered.find(marker, start)
            if index == -1:
                break
            before = lowered[index - 1] if index > 0 else " "
            after_index = index + len(marker)
            after = lowered[after_index] if after_index < len(lowered) else " "
            if not before.isalnum() and not after.isalnum():
                return True
            start = index + 1
    return False


def inferred_outcomes(sessions: Sequence[UnifiedSession]) -> MetricResult:
    """Heuristic success/failure tally over final assistant messages (``inferred``).

    Only produced when ``compute_workflow_metrics(include_inferred=True)`` is
    called; the class is ``inferred`` so it can never be added to an observed
    number.
    """
    source = (
        "UnifiedMessage.role",
        "UnifiedMessage.kind",
        "UnifiedMessage.content",
    )
    total = len(sessions)
    tally = {"succeeded": 0, "failed": 0, "unclassified": 0}
    for session in sessions:
        text = _last_assistant_text(session)
        if text is None:
            continue
        success = _marker_hit(text, SUCCESS_MARKERS)
        failure = _marker_hit(text, FAILURE_MARKERS)
        if success and not failure:
            tally["succeeded"] += 1
        elif failure and not success:
            tally["failed"] += 1
        else:
            tally["unclassified"] += 1
    covered = sum(tally.values())
    if covered == 0:
        return _absent(
            "inferred_outcomes",
            INFERRED,
            source,
            total,
            f"no session has a non-empty final assistant message ({total} session(s) seen)",
        )
    return MetricResult(
        name="inferred_outcomes",
        value=tally,
        kind=INFERRED,
        source_fields=source,
        covered_sessions=covered,
        total_sessions=total,
        caveats=(
            "INFERRED: keyword match on the final assistant message "
            f"({', '.join(SUCCESS_MARKERS)} vs {', '.join(FAILURE_MARKERS)}); "
            "a mention of either set, or both, yields 'unclassified'",
            "this is a judgement about intent, not an observed outcome; it is "
            "never summed with observed or computed metrics",
        ),
    )


def compute_workflow_metrics(
    sessions: Iterable[UnifiedSession], *, include_inferred: bool = False
) -> List[MetricResult]:
    """Compute the fixed metric set over ``sessions`` (read-only, deterministic).

    ``include_inferred`` is the explicit request the contract requires before an
    inferred metric may be produced at all.
    """
    session_list = list(sessions)
    results = [
        sessions_by_origin(session_list),
        prompts_per_session(session_list),
        messages_per_session(session_list),
        tool_call_failure_rate(session_list),
        median_session_duration_seconds(session_list),
    ]
    if include_inferred:
        results.append(inferred_outcomes(session_list))
    return results


def metrics_as_payload(results: Sequence[MetricResult]) -> Dict[str, Any]:
    """Machine-readable payload; every metric keeps its own ``kind``."""
    return {
        "total_sessions": results[0].total_sessions if results else 0,
        "metric_kinds": list(METRIC_KINDS),
        "metrics": [result.as_dict() for result in results],
    }


def format_metrics(results: Sequence[MetricResult]) -> str:
    """Human-readable report, grouped by class so kinds are never blurred."""
    total = results[0].total_sessions if results else 0
    lines = [f"Workflow metrics ({total} session(s), read-only)", "=" * 40]
    for want in METRIC_KINDS:
        group = [result for result in results if result.kind == want]
        if not group:
            continue
        lines.append("")
        lines.append(f"[{want}]")
        for result in group:
            value = "n/a" if result.value is None else result.value
            coverage = f"{result.covered_sessions}/{result.total_sessions}"
            lines.append(f"  {result.name}: {value}  (coverage {coverage})")
            lines.append(f"    source: {', '.join(result.source_fields)}")
            for caveat in result.caveats:
                lines.append(f"    caveat: {caveat}")
    return "\n".join(lines)
