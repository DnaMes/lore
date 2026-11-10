import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from lore.utils.formatting import format_message as format_message_with_rules

from .session_view_prep import fold_tool_bursts, render_message_body, strip_tool_blocks
from .web_formatting import format_session_event

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
LOCAL_COMMAND_TAG_RE = re.compile(r"</?local-command-[^>]+>", flags=re.IGNORECASE)
GENERIC_XML_TAG_RE = re.compile(r"</?[a-z0-9_-]+(?:\s+[^>]*)?>", flags=re.IGNORECASE)


FILE_UPDATE_PATTERNS = [
    re.compile(
        r"^the file\s+(.+?)\s+has been\s+(updated|created|deleted|modified)\.?$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(updated|created|deleted|modified)\s+file\s*:?\s+(.+?)\.?$",
        flags=re.IGNORECASE,
    ),
]


DEFAULT_NOISE_RULES: Dict[str, Dict[str, bool]] = {
    "default": {
        "strip_ansi_sequences": True,
        "session_classify_injected_events": True,
    },
    "claude-code": {
        "claude_strip_command_xml": True,
        "claude_summarize_file_operations": True,
    },
    "cursor": {
        "cursor_summarize_file_operations": True,
    },
    "gemini-cli": {
        "gemini_summarize_file_operations": True,
    },
    "codex": {
        "codex_summarize_file_operations": True,
    },
    "copilot-cli": {
        "copilot_summarize_file_operations": True,
    },
    "vscode-copilot": {
        "vscode_summarize_file_operations": True,
    },
    "opencode": {
        "opencode_strip_user_caveat": True,
        "opencode_summarize_file_operations": True,
        "opencode_summarize_unset_env_vars": True,
    },
    "antigravity": {
        "antigravity_summarize_file_operations": True,
    },
}


def compute_top_tags(
    all_sessions: List[Dict[str, Any]],
    limit: int = 12,
    user_tag_counts: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Most used tags for the filter dropdown.

    ``user_tag_counts`` (``{tag: session_count}`` from
    :func:`lore.storage.list_all_tags`) is merged in and ranked first, so a tag
    the user saved is selectable even when no LLM keyword repeats it (#173).
    """
    tag_counts: Dict[str, int] = {}
    for session in all_sessions:
        for keyword in session.get("keywords", [])[:30]:
            tag_counts[keyword] = tag_counts.get(keyword, 0) + 1
    for tag, count in (user_tag_counts or {}).items():
        # User tags outrank keyword noise of the same frequency.
        tag_counts[tag] = tag_counts.get(tag, 0) + int(count) + 1
    return [
        keyword
        for keyword, _ in sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def build_projects_payload(
    filtered_sessions: List[Dict[str, Any]], project_label_fn
) -> List[Dict[str, Any]]:
    by_project: Dict[str, Dict[str, Any]] = {}
    for session in filtered_sessions:
        project_path = session.get("project") or ""
        key = project_path or "Unassigned"
        entry = by_project.setdefault(
            key,
            {
                "path": project_path or "Unassigned",
                "sessions": [],
                "tools": set(),
                "tool_counts": {},
                "prompt_count": 0,
                "updated": "",
            },
        )
        entry["sessions"].append(session)
        entry["tools"].add(session.get("tool"))
        entry["tool_counts"][session.get("tool")] = (
            entry["tool_counts"].get(session.get("tool"), 0) + 1
        )
        entry["prompt_count"] += session.get("prompts", session.get("messages", 0))
        updated = session.get("updated") or session.get("created") or ""
        if updated > entry["updated"]:
            entry["updated"] = updated

    projects = []
    for _, entry in by_project.items():
        sessions_sorted = sorted(
            entry["sessions"],
            key=lambda session: session.get("updated") or session.get("created") or "",
            reverse=True,
        )
        tool_counts_sorted = sorted(
            entry["tool_counts"].items(), key=lambda item: item[1], reverse=True
        )
        projects.append(
            {
                "name": project_label_fn(entry["path"]),
                "path": entry["path"],
                "session_count": len(entry["sessions"]),
                "prompt_count": entry["prompt_count"],
                "tools": sorted([tool for tool in entry["tools"] if tool]),
                "tool_counts": dict(tool_counts_sorted),
                "agents": [{"tool": tool, "count": count} for tool, count in tool_counts_sorted],
                "updated": entry["updated"][:10] if entry["updated"] else "n/a",
                "recent_sessions": sessions_sorted[:3],
            }
        )
    return sorted(projects, key=lambda item: item.get("updated", ""), reverse=True)


def _normalize_message_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _clean_transcript_noise(text: str) -> str:
    cleaned = LOCAL_COMMAND_TAG_RE.sub("", text or "")
    cleaned = re.sub(r"\[Tool Result\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*Prompt\s*$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r"^\s*No files found\s*$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = _normalize_message_text(cleaned)
    return cleaned


_COMMAND_NOISE_LINE_RE = re.compile(
    r"^(?:\[command:[^\]]*\]|\[/?[a-z0-9_-]+\]|\(\s*\))$",
    flags=re.IGNORECASE,
)


def _is_command_only_noise(text: str) -> bool:
    """True if every non-empty line is command bookkeeping, e.g.::

        [command: /exit]
        [exit]
        ()

    Such a message carries no conversational content (#68).
    """
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    if not lines:
        return False
    return all(_COMMAND_NOISE_LINE_RE.match(line) for line in lines)


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
class _DisplayKind:
    kind: str  # "prompt", "event", "slash_prompt"
    label: str = ""
    prompt_title: str = ""


def _strip_single_wrapper(text: str, tag: str) -> str:
    pattern = re.compile(rf"^<{tag}>\s*(.*)\s*</{tag}>$", flags=re.DOTALL | re.IGNORECASE)
    match = pattern.match((text or "").strip())
    return match.group(1).strip() if match else ""


def _is_only_wrapped_tag(text: str, tag: str) -> bool:
    pattern = re.compile(rf"^<{tag}>\s*.*\s*</{tag}>$", flags=re.DOTALL | re.IGNORECASE)
    return bool(pattern.match((text or "").strip()))


def _slash_prompt_title(original: str) -> Optional[str]:
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


def _classify_injected_display(
    tool: str,
    role: str,
    original: str,
    noise_rules: Dict[str, Any],
) -> _DisplayKind:
    """Table-driven display classification for injected transcript noise."""
    if not _rule_enabled(noise_rules, "session_classify_injected_events", True):
        return _DisplayKind("prompt")

    text = (original or "").strip()
    if not text:
        return _DisplayKind("prompt")

    slash_title = _slash_prompt_title(text)
    if slash_title and role == "user":
        without_cmd = re.sub(
            r"<command-(?:message|name|args)>.*?</command-(?:message|name|args)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        without_cmd = re.sub(r"\s+", " ", without_cmd).strip()
        if not without_cmd:
            return _DisplayKind("slash_prompt", prompt_title=slash_title)

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
            return _DisplayKind("event", label=label_fn())
    return _DisplayKind("prompt")


def _is_toc_noise(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True
    noise_values = {
        "prompt",
        "[tool result]",
        "tool result",
        "no files found",
        "ok",
        "done",
    }
    if normalized in noise_values:
        return True
    if normalized.startswith("<local-command-"):
        return True
    return False


def _summarize_file_operations(text: str) -> str:
    if "File operations summary (collapsed):" in text:
        return text  # already folded: repeated normalisation stays idempotent
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    operations = []
    passthrough = []

    for line in lines:
        matched = None
        for pattern in FILE_UPDATE_PATTERNS:
            matched = pattern.match(line)
            if matched:
                break
        if not matched:
            passthrough.append(line)
            continue

        if matched.re is FILE_UPDATE_PATTERNS[0]:
            file_path = matched.group(1).strip("` ")
            action = matched.group(2).lower()
        else:
            action = matched.group(1).lower()
            file_path = matched.group(2).strip("` ")
        operations.append((action, file_path))

    if len(operations) < 3:
        return text

    preview = [f"- {action}: `{path}`" for action, path in operations[:8]]
    remainder = len(operations) - len(preview)
    if remainder > 0:
        preview.append(f"- ... and {remainder} more file operations")

    summary = "File operations summary (collapsed):\n" + "\n".join(preview)
    if passthrough and len(passthrough) <= 3:
        summary += "\n\n" + "\n".join(passthrough)
    # (#167) Display normalisation must not become a second lossy parser: the
    # lines it folds away stay one click away, verbatim.
    summary += (
        f"\n\n<details><summary>Show all {len(operations)} file operations</summary>\n\n"
        "```text\n"
        f"{text}\n"
        "```\n\n</details>"
    )
    return _normalize_message_text(summary)


def _summarize_unset_env_vars(text: str) -> str:
    if "Unset environment variables (" in text and "<details>" in text:
        return text  # already folded
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    keys = []
    passthrough = []
    pattern = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*:\s*not set\s*$", flags=re.IGNORECASE)

    for line in lines:
        matched = pattern.match(line)
        if not matched:
            passthrough.append(line)
            continue
        keys.append(matched.group(1).upper())

    if len(keys) < 4:
        return text

    shown = ", ".join(keys[:10])
    remainder = len(keys) - min(len(keys), 10)
    if remainder > 0:
        shown = f"{shown}, +{remainder} more"

    summary = f"Unset environment variables ({len(keys)}): {shown}"
    if passthrough and len(passthrough) <= 2:
        summary += "\n" + "\n".join(passthrough)
    summary += (
        f"\n\n<details><summary>Show all {len(keys)} unset variables</summary>\n\n"
        "```text\n"
        f"{text}\n"
        "```\n\n</details>"
    )
    return _normalize_message_text(summary)


def _rule_enabled(noise_rules: Dict[str, Any], key: str, default: bool) -> bool:
    value = noise_rules.get(key)
    if isinstance(value, bool):
        return value
    return default


def _format_message_for_tool(
    tool: str,
    role: str,
    content: str,
    noise_rules: Dict[str, Any],
) -> str:
    if not content:
        return ""

    text = content
    if _rule_enabled(noise_rules, "strip_ansi_sequences", True):
        text = ANSI_ESCAPE_RE.sub("", text)
    text = _normalize_message_text(text)
    text = _clean_transcript_noise(text)

    if tool == "claude-code" and _rule_enabled(noise_rules, "claude_strip_command_xml", True):

        def repl_cmd_name(m):
            return f"[command: {m.group(1)}]"

        def repl_cmd_msg(m):
            return f"[{m.group(1)}]"

        def repl_cmd_args(m):
            return f"({m.group(1)})"

        text = re.sub(r"<command-name>(.*?)</command-name>", repl_cmd_name, text, flags=re.DOTALL)
        text = re.sub(
            r"<command-message>(.*?)</command-message>",
            repl_cmd_msg,
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"<command-args>(.*?)</command-args>", repl_cmd_args, text, flags=re.DOTALL)
        text = _normalize_message_text(text)
        # The XML→[command:] rewrite can leave a message that is *only* command
        # bookkeeping — e.g. "[command: /exit]\n[exit]\n()". Such a message has
        # no conversational content; fold it away so it doesn't render as a
        # standalone prompt card (#68).
        if _is_command_only_noise(text):
            text = ""

    # Claude Code prepends the same "Caveat: the messages below…" boilerplate to
    # local-command user messages as OpenCode does; strip it for both (#68).
    if (
        tool in ("opencode", "claude-code")
        and role == "user"
        and _rule_enabled(noise_rules, "opencode_strip_user_caveat", True)
    ):
        lowered = text.lower()
        caveat = "caveat: the messages below were generated by the user"
        if caveat in lowered:
            idx = lowered.find(caveat)
            text = text[idx + len(caveat) :].strip()
            text = _normalize_message_text(text)

    summarize_rules = {
        "opencode": "opencode_summarize_file_operations",
        "claude-code": "claude_summarize_file_operations",
        "cursor": "cursor_summarize_file_operations",
        "vscode-copilot": "vscode_summarize_file_operations",
        "codex": "codex_summarize_file_operations",
        "copilot-cli": "copilot_summarize_file_operations",
        "gemini-cli": "gemini_summarize_file_operations",
        "antigravity": "antigravity_summarize_file_operations",
    }
    summarize_rule = summarize_rules.get(tool)
    if role == "assistant" and summarize_rule and _rule_enabled(noise_rules, summarize_rule, False):
        text = _summarize_file_operations(text)

    if tool == "opencode" and _rule_enabled(noise_rules, "opencode_summarize_unset_env_vars", True):
        text = _summarize_unset_env_vars(text)

    text = format_message_with_rules(text, tool=tool)
    return _normalize_message_text(text)


def normalize_session_messages_for_display(
    session, noise_rules: Dict[str, Dict[str, Any]] | None = None
) -> None:
    tool = getattr(getattr(session, "tool", None), "value", "")
    configured_rules = noise_rules or DEFAULT_NOISE_RULES
    shared_rules = configured_rules.get("default", {})
    provider_specific = configured_rules.get(tool, {}) if tool else {}
    provider_rules = dict(shared_rules)
    provider_rules.update(provider_specific)
    for message in getattr(session, "messages", []):
        role = getattr(getattr(message, "role", None), "value", "")
        # (#167) Display normalisation changes the view only. The canonical text
        # is preserved on the message and every run re-derives from it, which
        # also makes repeated normalisation idempotent.
        original = getattr(message, "original_content", None)
        if original is None:
            original = message.content or ""
            try:
                message.original_content = original
            except AttributeError:  # pragma: no cover - frozen message objects
                pass
        message.content = _format_message_for_tool(
            tool,
            role,
            original,
            provider_rules,
        )


def preview_normalize_message(
    tool: str,
    role: str,
    content: str,
    noise_rules: Dict[str, Dict[str, Any]] | None = None,
) -> str:
    normalized_tool = str(tool or "").strip()
    normalized_role = str(role or "assistant").strip().lower()
    if normalized_role not in {"assistant", "user", "system", "tool"}:
        normalized_role = "assistant"

    configured_rules = noise_rules or DEFAULT_NOISE_RULES
    shared_rules = configured_rules.get("default", {})
    provider_specific = configured_rules.get(normalized_tool, {}) if normalized_tool else {}
    provider_rules = dict(shared_rules)
    provider_rules.update(provider_specific)

    return _format_message_for_tool(
        normalized_tool,
        normalized_role,
        content or "",
        provider_rules,
    )


def _is_tool_result_record(message) -> bool:
    """True for a user-role record whose text is a ``[Tool Result]`` block (Claude tool_result)."""
    original = getattr(message, "original_content", None) or message.content or ""
    return original.lstrip().startswith("[Tool Result]")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_TOOL_ONLY_RE = re.compile(r"^\[Tool:\s*([^\]]+)\]\s*(.*)\Z", flags=re.DOTALL)
_EXIT_CODE_RE = re.compile(r"^\s*Exit code (\d+)")
_EXPORT_TOOL_STATUS_RE = re.compile(r"<summary>Tool:[^<]*?\u2014\s*(\w+)\s*</summary>")


def _flat_tool_call(message) -> Optional[Dict[str, Any]]:
    """Parse a record that is nothing but one flattened ``[Tool: X] args`` block.

    The export-markdown path keeps tool calls only as text (no structured ``tool_calls``);
    returns ``None`` for anything with prose or several calls.
    """
    raw = (getattr(message, "original_content", None) or message.content or "").strip()
    match = _TOOL_ONLY_RE.match(raw)
    if not match or "[Tool:" in match.group(2):
        return None
    body = match.group(2)
    status = _EXPORT_TOOL_STATUS_RE.search(body)
    return {
        "tool": match.group(1).strip(),
        "input": body.split("<details>", 1)[0].strip(),
        # None, not "": the export markdown has no output here — the result
        # arrives in a later record and may still fill it.
        "output": None,
        "status": status.group(1) if status else None,
    }


def _tool_only_calls(message) -> Optional[List[Dict[str, Any]]]:
    """Tool calls of an assistant record that has no prose or reasoning, else ``None``.

    Structured ``tool_calls`` (v2 store, live) get their compact event's status; a
    flattened ``[Tool: X]`` record (export markdown) is parsed from its text.
    """
    if message.display_role != "assistant" or message.reasoning:
        return None
    structured = getattr(message, "tool_calls", None)
    if not structured:
        flat = _flat_tool_call(message)
        return [flat] if flat else None
    if strip_tool_blocks(message.content or "").strip():
        return None
    events = getattr(message, "tool_events", None) or []
    aligned = len(events) == len(structured)
    calls = []
    for index, raw in enumerate(structured):
        call = dict(raw) if isinstance(raw, dict) else {"tool": str(raw), "input": {}}
        event = events[index] if aligned and isinstance(events[index], dict) else None
        if event:
            if call.get("status") is None:
                call["status"] = event.get("status")
            if call.get("duration_ms") is None:
                call["duration_ms"] = event.get("duration_ms")
            call["is_error"] = bool(call.get("is_error") or event.get("is_error"))
        calls.append(call)
    return calls


def _attach_result(calls: List[Dict[str, Any]], output: str) -> bool:
    """Give ``output`` to the first call still without one; False when all have output."""
    # An empty output is a real output: only a missing/None one may be filled,
    # or a later result would overwrite the first call's (empty) output.
    target = next((call for call in calls if call.get("output") is None), None)
    if target is None:
        return False
    target["output"] = output
    exit_match = _EXIT_CODE_RE.match(output)
    if exit_match:  # Claude prefixes a failed Bash result with "Exit code N"
        target.update(status="failed", exit_code=int(exit_match.group(1)))
    elif output.lstrip().startswith("<tool_use_error>"):
        target["status"] = "failed"
    return True


def group_flat_tool_steps(pairs: List[Dict[str, Any]], format_tool_calls_fn) -> None:
    """Fold runs of tool-only records and their result records into one tool group.

    The first record of a run carries the group HTML; the rest get ``merged_into_group``
    and render nothing, so find-in-session positions stay aligned with ``responses``.
    A result record whose output a structured call already shows is hidden the same way.
    """
    for pair in pairs:
        run: List[Any] = []
        calls: List[Dict[str, Any]] = []
        shown_outputs = False  # the previous record's cards already show their results

        def _flush() -> None:
            if calls:
                run[0].formatted_content = format_tool_calls_fn(fold_tool_bursts(list(calls)))
                for merged in run[1:]:
                    merged.merged_into_group = True
            run.clear()
            calls.clear()

        for resp in pair["responses"]:
            if resp.display_role == "tool":  # a tool role row or a Claude "[Tool Result]" user row
                original = getattr(resp, "original_content", None) or resp.content or ""
                output = re.sub(r"^\s*\[Tool Result\]\s*", "", original)
                if calls:
                    if _attach_result(calls, output) or shown_outputs:
                        run.append(resp)
                        continue
                elif shown_outputs:
                    resp.merged_into_group = True
                    continue
                _flush()
                continue
            new_calls = _tool_only_calls(resp)
            if new_calls:
                run.append(resp)
                calls.extend(new_calls)
                shown_outputs = any(call.get("output") for call in new_calls)
                continue
            _flush()
            shown_outputs = bool(getattr(resp, "tool_calls", None))
        _flush()
        pair["step_count"] = sum(
            1 for resp in pair["responses"] if not getattr(resp, "merged_into_group", False)
        )


def annotate_turns(pairs: List[Dict[str, Any]]) -> None:
    """Add per-turn display fields: ``day_label`` when the date changes, model and token sums.

    Computed over the whole session so a lazily loaded page only shows a day separator
    where the date really changes.
    """
    previous_day = None
    for pair in pairs:
        first = pair["user"] or (pair["responses"][0] if pair["responses"] else None)
        stamp = getattr(first, "timestamp", None)
        day = stamp.date() if stamp else None
        pair["day_label"] = stamp.strftime("%A, %Y-%m-%d") if day and day != previous_day else ""
        if day:
            previous_day = day
        tokens_in = tokens_out = 0
        model = ""
        for resp in pair["responses"]:
            tokens = getattr(resp, "tokens", None)
            if isinstance(tokens, dict):
                tokens_in += _as_int(tokens.get("input"))
                tokens_out += _as_int(tokens.get("output"))
            model = model or (getattr(resp, "model", None) or "")
        pair["tokens_in"] = tokens_in
        pair["tokens_out"] = tokens_out
        pair["model"] = model


def enrich_session_for_detail(
    session,
    session_meta: Dict[str, Any],
    format_message_content_fn,
    format_tool_calls_fn,
    format_thinking_fn,
    noise_rules=None,
):
    normalize_session_messages_for_display(session, noise_rules=noise_rules)

    if session_meta:
        if session_meta.get("title") and not session.title:
            session.title = session_meta["title"]
        if session_meta.get("project") and not session.project_path:
            session.project_path = session_meta["project"]
        if session_meta.get("thread_id") and not session.thread_id:
            session.thread_id = session_meta["thread_id"]

    tool = getattr(getattr(session, "tool", None), "value", "")
    configured_rules = noise_rules or DEFAULT_NOISE_RULES
    shared_rules = configured_rules.get("default", {})
    provider_specific = configured_rules.get(tool, {}) if tool else {}
    provider_rules = dict(shared_rules)
    provider_rules.update(provider_specific)

    toc = []
    visible_count = 0
    prompt_count = 0
    pairs = []
    current_pair = None

    def _toc_text(raw_text: str) -> str:
        text = _clean_transcript_noise(raw_text or "")
        text = re.sub(r"\[Tool.*?\]", "", text)
        text = GENERIC_XML_TAG_RE.sub("", text)
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if _is_toc_noise(text):
            return ""
        if not text:
            return "Prompt"
        return text[:88]

    for idx, message in enumerate(session.messages):
        message.formatted_reasoning = ""
        if message.reasoning:
            message.formatted_reasoning = format_thinking_fn(message.reasoning)
        # A user record that only carries a tool result belongs to the agent turn, not a prompt.
        message.display_role = message.role.value
        message.merged_into_group = False
        if message.role.value == "user" and _is_tool_result_record(message):
            message.display_role = "tool"

        original = getattr(message, "original_content", None) or message.content or ""
        injected = _classify_injected_display(
            tool,
            message.role.value,
            original,
            provider_rules,
        )
        if injected.kind == "event":
            message.display_role = "event"
            message.formatted_content = format_session_event(injected.label, original)
            visible_count += 1
            message.index = idx
            if not current_pair:
                current_pair = {"user": None, "responses": []}
            current_pair["responses"].append(message)
            continue

        if message.display_role == "user":
            prompt_count += 1
            toc_source = (
                injected.prompt_title if injected.kind == "slash_prompt" else message.content
            )
            text = _toc_text(toc_source)
            if text:
                toc.append(
                    {
                        "index": idx,
                        "text": text,
                        "time": message.timestamp.strftime("%H:%M"),
                        "role": "user",
                    }
                )
            if current_pair:
                pairs.append(current_pair)
            current_pair = {"user": message, "responses": []}

        if injected.kind == "slash_prompt" and message.display_role == "user":
            message.formatted_content = format_message_content_fn(injected.prompt_title)
        else:
            # Render from STRUCTURED tool_calls when present (prose + paired tool
            # cards) instead of re-parsing tool blocks out of the flattened string.
            # See session_view_prep for the why (docs/UMBAU.md §0).
            message.formatted_content = render_message_body(
                message,
                format_message_content_fn=format_message_content_fn,
                format_tool_calls_fn=format_tool_calls_fn,
            )

        if message.display_role == "tool" and message.role.value == "user":
            # Render the raw block so it becomes a folded "Tool result" card, not prose.
            message.formatted_content = format_message_content_fn(
                getattr(message, "original_content", None) or message.content
            )

        if message.formatted_content:
            message.formatted_content = _clean_transcript_noise(message.formatted_content)

        if message.content or message.reasoning or message.formatted_content:
            visible_count += 1
            message.index = idx
            if message.display_role != "user":
                if not current_pair:
                    current_pair = {"user": None, "responses": []}
                current_pair["responses"].append(message)

    if current_pair:
        pairs.append(current_pair)
    group_flat_tool_steps(pairs, format_tool_calls_fn)
    annotate_turns(pairs)

    session.visible_count = visible_count
    session.prompt_count = prompt_count
    session.pairs = pairs
    return toc


def _group_threads_by_id(index_sessions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Bucket ``index_sessions`` into one aggregate overview dict per thread."""
    by_thread: Dict[str, Dict[str, Any]] = {}
    for session in index_sessions:
        thread_id = session.get("thread_id")
        if not thread_id:
            continue
        if thread_id not in by_thread:
            by_thread[thread_id] = {
                "thread_id": thread_id,
                "title": session.get("title") or session.get("id"),
                "project": session.get("project"),
                "count": 0,
                "updated": session.get("updated"),
            }
        by_thread[thread_id]["count"] += 1
        if session.get("updated", "") > by_thread[thread_id]["updated"]:
            by_thread[thread_id]["updated"] = session.get("updated")
            by_thread[thread_id]["title"] = session.get("title") or session.get("id")
            by_thread[thread_id]["project"] = session.get("project")
    return by_thread


def build_threads_overview(
    index_sessions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return sorted(
        _group_threads_by_id(index_sessions).values(),
        key=lambda item: item.get("updated", ""),
        reverse=True,
    )


# Memoized {thread_id: overview} bucketing over one sessions list (#126).
# A thread's overview aggregates every session in the thread, so the O(n)
# grouping pass is inherent — but get_thread (MCP) used to redo it per call
# just to locate a single bucket. Like the session by-id map, the memo keys
# on the sessions-list object: a rebuilt index (or a test fake) yields a
# different list and rebuilds the buckets. The strong reference in the memo
# keeps the identity comparison safe.
_THREAD_BY_ID_MEMO: Optional[tuple[list, Dict[str, Dict[str, Any]]]] = None


def thread_overview_by_id(
    index_sessions: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return ``{thread_id: overview}`` for ``index_sessions``, memoized.

    Overviews are shared (not copies) with :func:`build_threads_overview`
    callers reading the same list — treat them as read-only.
    """
    global _THREAD_BY_ID_MEMO
    memo = _THREAD_BY_ID_MEMO
    if memo is None or memo[0] is not index_sessions:
        memo = (index_sessions, _group_threads_by_id(index_sessions))
        _THREAD_BY_ID_MEMO = memo
    return memo[1]


def build_thread_detail_payload(
    thread_id: str,
    index_sessions: List[Dict[str, Any]],
    load_sessions_for_tool_fn,
    format_message_content_fn,
    format_tool_calls_fn,
    get_style_fn,
    noise_rules=None,
    sanitize_rendered_html_fn=None,
) -> Dict[str, Any]:
    thread_sessions = [
        session for session in index_sessions if session.get("thread_id") == thread_id
    ]
    thread_sessions = sorted(thread_sessions, key=lambda session: session.get("created", ""))

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for session in thread_sessions:
        tool = str(session.get("tool") or "")
        grouped.setdefault(tool, []).append(session)

    thread_groups = sorted(
        [
            {"tool": tool, "count": len(sessions), "sessions": sessions[:5]}
            for tool, sessions in grouped.items()
        ],
        key=lambda item: item["tool"] or "",
    )

    tool_filters = sorted({str(s.get("tool")) for s in thread_sessions if s.get("tool")})
    target_ids_by_tool: Dict[str, set[str]] = {}
    for s in thread_sessions:
        tool = str(s.get("tool") or "")
        sid = str(s.get("id") or "")
        if tool and sid:
            target_ids_by_tool.setdefault(tool, set()).add(sid)

    messages = []
    session_count = 0
    tool_set = set()
    for tool in tool_filters:
        target_ids = target_ids_by_tool.get(tool, set())
        if not target_ids:
            continue

        sessions = load_sessions_for_tool_fn(tool)
        found_ids: set[str] = set()

        for session in sessions:
            sid = str(getattr(session, "session_id", "") or "")
            if not sid or sid not in target_ids:
                continue

            if sid in found_ids:
                continue
            found_ids.add(sid)

            normalize_session_messages_for_display(session, noise_rules=noise_rules)

            session_count += 1
            tool_set.add(session.tool.value)
            for message in session.messages:
                content = render_message_body(
                    message,
                    format_message_content_fn=format_message_content_fn,
                    format_tool_calls_fn=format_tool_calls_fn,
                )
                if sanitize_rendered_html_fn and content:
                    content = sanitize_rendered_html_fn(content)
                label = (
                    "You"
                    if message.role.value == "user"
                    else get_style_fn(session.tool.value)["name"]
                )
                messages.append(
                    {
                        "role": message.role,
                        "timestamp": message.timestamp,
                        "content": content,
                        "raw_content": message.content or "",
                        "label": label,
                        "style": get_style_fn(session.tool.value),
                    }
                )

            if len(found_ids) >= len(target_ids):
                break

    messages.sort(key=lambda message: message["timestamp"])
    toc_items = []
    for idx, message in enumerate(messages):
        if message["role"].value == "user" and message["raw_content"]:
            preview = re.sub(r"\s+", " ", message["raw_content"]).strip()
            if preview:
                toc_items.append(
                    {
                        "index": idx,
                        "text": preview[:60],
                        "time": message["timestamp"].strftime("%H:%M"),
                        "role": "user",
                    }
                )

    continue_cmd = f"lore-session switch gemini --thread-id {thread_id}"
    thread_meta = {
        "session_count": session_count,
        "message_count": len(messages),
        "tools": ", ".join(sorted(tool_set)) if tool_set else "n/a",
        "start_date": (messages[0]["timestamp"].strftime("%Y-%m-%d") if messages else "n/a"),
        "end_date": (messages[-1]["timestamp"].strftime("%Y-%m-%d") if messages else "n/a"),
    }

    return {
        "thread_groups": thread_groups,
        "thread_timeline": thread_sessions,
        "messages": messages,
        "toc_items": toc_items,
        "continue_cmd": continue_cmd,
        "thread_meta": thread_meta,
    }
