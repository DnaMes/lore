"""Extract rules, and evidence-backed harness recommendations (#189).

Two layers live here:

``extract_rules``
    The original deterministic pass: rule-like assistant sentences, ranked by
    keyword hits.
``build_recommendations`` + the store helpers
    The reviewed layer. A recommendation carries the sentence, the ``scope`` it
    would apply to, the evidence links it came from (session id plus a
    ``session:<id>#m<seq>`` permalink), the generation request that produced it,
    an explicit ``state`` (``inactive`` until a human activates it) and an
    append-only evaluation history.

Nothing in this module writes harness configuration. Exporting, activating and
evaluating only touch the recommendation store; applying an active
recommendation to a harness stays a manual, out-of-band action.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..core.models import Role, UnifiedSession
from .paths import restrict_file
from .redact import redact

RULE_KEYWORDS = (
    "must",
    "should",
    "avoid",
    "do not",
    "never",
    "always",
    "best practice",
    "recommend",
    "prefer",
)
MIN_SENTENCE_LENGTH = 20
MAX_SENTENCE_LENGTH = 180

SCOPES = ("project", "tool", "global")
RECOMMENDATION_STATES = ("inactive", "active")
EVALUATION_VERDICTS = ("accepted", "corrected", "contradicted")

STORE_FILENAME = "recommendations.json"
STORE_FORMAT = "lore-recommendations"
STORE_FORMAT_VERSION = 1

# The generation request is recorded on every recommendation so an export can be
# reproduced: this generator is local and deterministic, hence no remote model.
GENERATOR_PROVIDER = "local"
GENERATOR_MODEL = "sentence-keyword-v1"
PROMPT_VERSION = "rules-extract-v1"


def _score_sentence(sentence: str) -> int:
    lowered = sentence.lower()
    return sum(1 for keyword in RULE_KEYWORDS if keyword in lowered)


def _iter_rule_candidates(
    sessions: Iterable[UnifiedSession],
) -> Iterator[Tuple[str, int, UnifiedSession, int]]:
    """Yield ``(sentence, score, session, message_seq)`` for rule-like assistant sentences."""
    for session in sessions:
        for message_seq, message in enumerate(session.messages):
            if message.role != Role.ASSISTANT:
                continue
            for raw in re.split(r"(?<=[.!?])\s+", message.content or ""):
                sentence = raw.strip()
                if not MIN_SENTENCE_LENGTH <= len(sentence) <= MAX_SENTENCE_LENGTH:
                    continue
                score = _score_sentence(sentence)
                if score > 0:
                    yield sentence, score, session, message_seq


def extract_rules(sessions: Iterable[UnifiedSession], max_rules: int = 30) -> List[str]:
    """Extract rule-like sentences from assistant messages."""
    scores: Dict[str, int] = {}
    order: List[str] = []
    for sentence, score, _session, _seq in _iter_rule_candidates(sessions):
        if sentence not in scores:
            order.append(sentence)
        scores[sentence] = max(scores.get(sentence, 0), score)
    ranked = sorted(enumerate(order), key=lambda pair: (-scores[pair[1]], pair[0]))
    return [sentence for _index, sentence in ranked[:max_rules]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GenerationRequest:
    """How a recommendation was produced — provider, model and prompt version."""

    provider: str
    model: str
    prompt_version: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerationRequest":
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            prompt_version=str(data.get("prompt_version", "")),
        )


DEFAULT_GENERATION = GenerationRequest(
    provider=GENERATOR_PROVIDER,
    model=GENERATOR_MODEL,
    prompt_version=PROMPT_VERSION,
)


@dataclass(frozen=True)
class EvidenceLink:
    """One supporting message: the session id plus a stable permalink."""

    session_id: str
    tool: str
    message_seq: int
    permalink: str
    quote: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tool": self.tool,
            "message_seq": self.message_seq,
            "permalink": self.permalink,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceLink":
        return cls(
            session_id=str(data.get("session_id", "")),
            tool=str(data.get("tool", "")),
            message_seq=int(data.get("message_seq", -1)),
            permalink=str(data.get("permalink", "")),
            quote=str(data.get("quote", "")),
        )


def evidence_permalink(session_id: str, message_seq: int) -> str:
    """``session:<id>#m<seq>`` — ``seq`` is the 0-based index of the message."""
    return f"session:{session_id}#m{message_seq}"


@dataclass(frozen=True)
class EvaluationEntry:
    """One append-only review event (accepted / corrected / contradicted)."""

    verdict: str
    at: str
    by: str
    note: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"verdict": self.verdict, "at": self.at, "by": self.by, "note": self.note}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvaluationEntry":
        return cls(
            verdict=str(data.get("verdict", "")),
            at=str(data.get("at", "")),
            by=str(data.get("by", "")),
            note=str(data.get("note", "")),
        )


@dataclass
class Recommendation:
    """A candidate harness rule, inactive until a human activates it."""

    id: str
    text: str
    scope: str
    scope_target: str
    evidence: List[EvidenceLink]
    generation: GenerationRequest
    state: str = "inactive"
    created_at: str = ""
    activated_by: Optional[str] = None
    activated_at: Optional[str] = None
    history: List[EvaluationEntry] = field(default_factory=list)

    @property
    def current_verdict(self) -> str:
        """Latest review verdict, or ``unreviewed`` before the first one."""
        return self.history[-1].verdict if self.history else "unreviewed"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "scope": self.scope,
            "scope_target": self.scope_target,
            "state": self.state,
            "created_at": self.created_at,
            "activated_by": self.activated_by,
            "activated_at": self.activated_at,
            "current_verdict": self.current_verdict,
            "generation": self.generation.as_dict(),
            "evidence": [link.as_dict() for link in self.evidence],
            "history": [entry.as_dict() for entry in self.history],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recommendation":
        state = str(data.get("state", "inactive"))
        if state not in RECOMMENDATION_STATES:
            raise ValueError(f"invalid recommendation state {state!r}")
        return cls(
            id=str(data["id"]),
            text=str(data.get("text", "")),
            scope=str(data.get("scope", "global")),
            scope_target=str(data.get("scope_target", "*")),
            evidence=[EvidenceLink.from_dict(item) for item in data.get("evidence", [])],
            generation=GenerationRequest.from_dict(data.get("generation") or {}),
            state=state,
            created_at=str(data.get("created_at", "")),
            activated_by=data.get("activated_by"),
            activated_at=data.get("activated_at"),
            history=[EvaluationEntry.from_dict(item) for item in data.get("history", [])],
        )


def recommendation_id(scope: str, scope_target: str, text: str) -> str:
    """Content-derived, stable id: re-exporting the same rule keeps its identity."""
    digest = hashlib.sha256(f"{scope}\x00{scope_target}\x00{text}".encode("utf-8")).hexdigest()
    return f"rec-{digest[:12]}"


def _scope_for(projects: set[str], tools: set[str], unknown_project: bool) -> Tuple[str, str]:
    """Project scope only when every piece of evidence names the same project."""
    if len(projects) == 1 and not unknown_project:
        return "project", next(iter(projects))
    if len(tools) == 1:
        return "tool", next(iter(tools))
    return "global", "*"


def build_recommendations(
    sessions: Iterable[UnifiedSession],
    max_recommendations: int = 30,
    *,
    now: Optional[str] = None,
) -> List[Recommendation]:
    """Build inactive recommendations with evidence links from ``sessions``.

    The rule text and every quoted snippet are redacted at build time, so no
    exported store can carry a secret-shaped string.
    """
    created_at = now or _now()
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for sentence, score, session, message_seq in _iter_rule_candidates(sessions):
        text = redact(sentence).text
        entry = groups.get(text)
        if entry is None:
            entry = {
                "score": score,
                "links": {},
                "projects": set(),
                "tools": set(),
                "unknown_project": False,
            }
            groups[text] = entry
            order.append(text)
        else:
            entry["score"] = max(entry["score"], score)
        link = EvidenceLink(
            session_id=session.session_id,
            tool=session.tool.value,
            message_seq=message_seq,
            permalink=evidence_permalink(session.session_id, message_seq),
            quote=text,
        )
        entry["links"].setdefault(link.permalink, link)
        entry["tools"].add(session.tool.value)
        if session.project_path:
            entry["projects"].add(session.project_path)
        else:
            entry["unknown_project"] = True

    ranked = sorted(enumerate(order), key=lambda pair: (-groups[pair[1]]["score"], pair[0]))
    recommendations: List[Recommendation] = []
    for _index, text in ranked[:max_recommendations]:
        entry = groups[text]
        scope, scope_target = _scope_for(
            entry["projects"], entry["tools"], entry["unknown_project"]
        )
        recommendations.append(
            Recommendation(
                id=recommendation_id(scope, scope_target, text),
                text=text,
                scope=scope,
                scope_target=scope_target,
                evidence=sorted(entry["links"].values(), key=lambda link: link.permalink),
                generation=DEFAULT_GENERATION,
                created_at=created_at,
            )
        )
    return recommendations


def merge_recommendations(
    existing: Sequence[Recommendation], fresh: Sequence[Recommendation]
) -> List[Recommendation]:
    """Keep review state and evidence; never drop a recommendation with history.

    Activation, activation metadata and the evaluation history come from the
    store. Evidence is unioned by permalink, so a re-export adds new supporting
    messages without erasing the ones a reviewer already saw. A stored
    recommendation whose evidence no longer appears in the fresh scan is kept
    as-is rather than deleted.
    """
    by_id = {recommendation.id: recommendation for recommendation in existing}
    merged: List[Recommendation] = []
    seen: set[str] = set()
    for new in fresh:
        old = by_id.get(new.id)
        seen.add(new.id)
        if old is None:
            merged.append(new)
            continue
        links = {link.permalink: link for link in old.evidence}
        for link in new.evidence:
            links.setdefault(link.permalink, link)
        merged.append(
            replace(
                new,
                evidence=sorted(links.values(), key=lambda link: link.permalink),
                state=old.state,
                created_at=old.created_at or new.created_at,
                activated_by=old.activated_by,
                activated_at=old.activated_at,
                history=list(old.history),
            )
        )
    merged.extend(item for item in existing if item.id not in seen)
    return merged


def _find(
    recommendations: Sequence[Recommendation], recommendation_id_value: str
) -> Recommendation:
    for recommendation in recommendations:
        if recommendation.id == recommendation_id_value:
            return recommendation
    raise KeyError(f"no recommendation with id {recommendation_id_value!r}")


def activate_recommendation(
    recommendations: Sequence[Recommendation],
    recommendation_id_value: str,
    *,
    by: str,
    now: Optional[str] = None,
) -> Recommendation:
    """Flip one recommendation to ``active`` and record who and when.

    Raises ``KeyError`` for an unknown id. Nothing is applied to any harness:
    the state change is the whole effect.
    """
    target = _find(recommendations, recommendation_id_value)
    target.state = "active"
    target.activated_by = by
    target.activated_at = now or _now()
    return target


def record_evaluation(
    recommendations: Sequence[Recommendation],
    recommendation_id_value: str,
    verdict: str,
    *,
    by: str,
    note: str = "",
    now: Optional[str] = None,
) -> Recommendation:
    """Append a review event; the original text, evidence and prior history stay."""
    if verdict not in EVALUATION_VERDICTS:
        raise ValueError(f"invalid verdict {verdict!r}; expected one of {EVALUATION_VERDICTS}")
    target = _find(recommendations, recommendation_id_value)
    target.history.append(EvaluationEntry(verdict=verdict, at=now or _now(), by=by, note=note))
    return target


def recommendations_payload(
    recommendations: Sequence[Recommendation], *, generated_at: Optional[str] = None
) -> Dict[str, Any]:
    """The portable, machine-readable export shape."""
    return {
        "format": STORE_FORMAT,
        "format_version": STORE_FORMAT_VERSION,
        "generated_at": generated_at or _now(),
        "recommendations": [recommendation.as_dict() for recommendation in recommendations],
    }


def recommendations_path(output_dir: Path) -> Path:
    return Path(output_dir) / STORE_FILENAME


def load_recommendations(path: Path) -> List[Recommendation]:
    """Read the store; an absent file is an empty store, a damaged one raises."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != STORE_FORMAT:
        raise ValueError(f"{path} is not a {STORE_FORMAT} file")
    if int(data.get("format_version", 0)) > STORE_FORMAT_VERSION:
        raise ValueError(
            f"{path} has format_version {data.get('format_version')}, "
            f"this Lore understands {STORE_FORMAT_VERSION}"
        )
    return [Recommendation.from_dict(item) for item in data.get("recommendations", [])]


def save_recommendations(
    path: Path,
    recommendations: Sequence[Recommendation],
    *,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the store (mode 0600) and return the payload that was written."""
    payload = recommendations_payload(recommendations, generated_at=generated_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    restrict_file(path)
    return payload


def format_recommendations(recommendations: Sequence[Recommendation]) -> str:
    """Human-readable review listing; every block states its state and evidence."""
    lines = [
        "# Harness recommendations",
        "",
        "Inactive until explicitly activated with 'lore rules activate <id>'.",
        "Nothing here modifies a harness configuration.",
        "",
    ]
    if not recommendations:
        lines.append("(no recommendations)")
        return "\n".join(lines)
    for recommendation in recommendations:
        lines.append(
            f"## {recommendation.id} [{recommendation.state}] "
            f"{recommendation.scope}:{recommendation.scope_target}"
        )
        lines.append(recommendation.text)
        generation = recommendation.generation
        lines.append(
            f"generation: {generation.provider} / {generation.model} / {generation.prompt_version}"
        )
        if recommendation.activated_at:
            lines.append(
                f"activated: {recommendation.activated_at} by {recommendation.activated_by}"
            )
        lines.append("evidence:")
        for link in recommendation.evidence:
            lines.append(f"  - {link.permalink} ({link.tool}): {link.quote}")
        for entry in recommendation.history:
            note = f" — {entry.note}" if entry.note else ""
            lines.append(f"history: {entry.verdict} by {entry.by} at {entry.at}{note}")
        lines.append("")
    return "\n".join(lines)
