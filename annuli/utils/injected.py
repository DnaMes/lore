"""Classify injected transcript noise (skills, slash commands, hooks) for display titles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

_CMD_NAME_RE = re.compile(
    r"<command-name>\s*(.*?)\s*</command-name>", flags=re.DOTALL | re.IGNORECASE
)
_CMD_ARGS_RE = re.compile(
    r"<command-args>\s*(.*?)\s*</command-args>", flags=re.DOTALL | re.IGNORECASE
)
_SKILL_DIR_RE = re.compile(r"^Base directory for this skill:\s*(\S+)", flags=re.IGNORECASE)
_HOOK_FEEDBACK_RE = re.compile(r"^(?:Stop|[A-Za-z][\w-]*) hook feedback:", flags=re.IGNORECASE)
_TOOLU_ECHO_RE = re.compile(
    r"^[0-9a-f]{6,}\s+toolu_[A-Za-z0-9]+\b", flags=re.IGNORECASE | re.DOTALL
)
_AGENTS_HEADER_RE = re.compile(r"^#\s*AGENTS\.md instructions\b", flags=re.IGNORECASE)
_CODEX_WRAP_TAGS = ("environment_context", "user_instructions", "user_action")


@dataclass(frozen=True)
class DisplayKind:
    kind: str  # "prompt", "event", "slash_prompt"
    label: str = ""
    prompt_title: str = ""


def rule_enabled(noise_rules: Dict[str, Any], key: str, default: bool) -> bool:
    value = noise_rules.get(key)
    if isinstance(value, bool):
        return value
    return default


def _strip_single_wrapper(text: str, tag: str) -> str:
    pattern = re.compile(rf"^<{tag}>\s*(.*)\s*</{tag}>$", flags=re.DOTALL | re.IGNORECASE)
    match = pattern.match((text or "").strip())
    return match.group(1).strip() if match else ""


def _is_only_wrapped_tag(text: str, tag: str) -> bool:
    pattern = re.compile(rf"^<{tag}>\s*.*\s*</{tag}>$", flags=re.DOTALL | re.IGNORECASE)
    return bool(pattern.match((text or "").strip()))


def slash_prompt_title(original: str) -> Optional[str]:
    name_match = _CMD_NAME_RE.search(original or "")
    if not name_match:
        return None
    name = name_match.group(1).strip()
    if not name:
        return None
    args_match = _CMD_ARGS_RE.search(original or "")
    args = args_match.group(1).strip() if args_match else ""
    if args:
        return f"{name} {args}".strip()
    return name


def _skill_event_label(original: str) -> str:
    match = _SKILL_DIR_RE.match(original or "")
    if not match:
        return "Skill loaded"
    slug = match.group(1).rstrip("/").rsplit("/", 1)[-1]
    return f"Skill loaded: {slug}"


def _codex_injected_label(original: str) -> str:
    lowered = (original or "").strip().lower()
    for tag in _CODEX_WRAP_TAGS:
        if lowered.startswith(f"<{tag}>"):
            return "System context"
    if _AGENTS_HEADER_RE.match(original or ""):
        return "AGENTS instructions"
    return "System prompt"


def classify_injected_display(
    tool: str,
    role: str,
    original: str,
    noise_rules: Dict[str, Any],
) -> DisplayKind:
    """Table-driven display classification for injected transcript noise."""
    if not rule_enabled(noise_rules, "session_classify_injected_events", True):
        return DisplayKind("prompt")

    text = (original or "").strip()
    if not text:
        return DisplayKind("prompt")

    slash_title = slash_prompt_title(text)
    if slash_title and role == "user":
        without_cmd = re.sub(
            r"<command-(?:message|name|args)>.*?</command-(?:message|name|args)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        without_cmd = re.sub(r"\s+", " ", without_cmd).strip()
        if not without_cmd:
            return DisplayKind("slash_prompt", prompt_title=slash_title)

    checks: list[tuple[Callable[[], bool], Callable[[], str]]] = [
        (
            lambda: role == "user" and bool(_SKILL_DIR_RE.match(text)),
            lambda: _skill_event_label(text),
        ),
        (
            lambda: role == "user" and bool(_HOOK_FEEDBACK_RE.match(text)),
            lambda: "Hook feedback",
        ),
        (
            lambda: role == "user" and _is_only_wrapped_tag(text, "task-notification"),
            lambda: "Task notification",
        ),
        (
            lambda: role == "user" and bool(_TOOLU_ECHO_RE.match(text)),
            lambda: "Task notification",
        ),
        (
            lambda: _is_only_wrapped_tag(text, "system-reminder"),
            lambda: "System reminder",
        ),
        (
            lambda: any(
                _is_only_wrapped_tag(text, tag)
                for tag in ("local-command-caveat", "local-command-stdout")
            ),
            lambda: "Local command",
        ),
        (
            lambda: (
                tool == "codex"
                and role in {"system", "user"}
                and (
                    any(_is_only_wrapped_tag(text, tag) for tag in _CODEX_WRAP_TAGS)
                    or (_AGENTS_HEADER_RE.match(text) and "<instructions>" in text.lower())
                    or (text.startswith("<!-- BEGIN AGENTS") and "<!-- END AGENTS" in text)
                )
            ),
            lambda: _codex_injected_label(text),
        ),
    ]
    for predicate, label_fn in checks:
        if predicate():
            return DisplayKind("event", label=label_fn())
    return DisplayKind("prompt")
