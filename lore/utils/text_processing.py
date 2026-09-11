import difflib
import html
import json
import re
from typing import Any


def _diff_rows(old: str, new: str, context: int = 3) -> tuple[list[dict], int, int]:
    """Compute unified-diff rows between two texts.

    Returns ``(rows, additions, deletions)`` where each row is
    ``{"kind": "context"|"add"|"del"|"gap", "old": int|None, "new": int|None,
    "text": str}``. Long runs of unchanged lines collapse to a single ``gap``
    row carrying the hidden count in ``text`` (UMBAU §5).
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[dict] = []
    additions = deletions = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            run = i2 - i1
            # Collapse the middle of long unchanged runs, keep `context` lines
            # of padding on each side so changes stay readable.
            if run > 2 * context + 1:
                for k in range(context):
                    rows.append(
                        {
                            "kind": "context",
                            "old": i1 + k + 1,
                            "new": j1 + k + 1,
                            "text": old_lines[i1 + k],
                        }
                    )
                hidden = run - 2 * context
                rows.append(
                    {"kind": "gap", "old": None, "new": None, "text": f"{hidden} unchanged lines"}
                )
                for k in range(run - context, run):
                    rows.append(
                        {
                            "kind": "context",
                            "old": i1 + k + 1,
                            "new": j1 + k + 1,
                            "text": old_lines[i1 + k],
                        }
                    )
            else:
                for k in range(run):
                    rows.append(
                        {
                            "kind": "context",
                            "old": i1 + k + 1,
                            "new": j1 + k + 1,
                            "text": old_lines[i1 + k],
                        }
                    )
        else:
            for k in range(i1, i2):
                rows.append({"kind": "del", "old": k + 1, "new": None, "text": old_lines[k]})
                deletions += 1
            for k in range(j1, j2):
                rows.append({"kind": "add", "old": None, "new": k + 1, "text": new_lines[k]})
                additions += 1
    return rows, additions, deletions


def _render_diff_rows(rows: list[dict]) -> str:
    """Render diff rows to an HTML table. Inputs are HTML-escaped here."""
    out = []
    for row in rows:
        kind = row["kind"]
        if kind == "gap":
            out.append(
                f'<div class="diff-row diff-gap"><span class="diff-gap-label">'
                f"⋯ {html.escape(row['text'])} ⋯</span></div>"
            )
            continue
        marker = {"add": "+", "del": "-", "context": " "}[kind]
        old_no = "" if row["old"] is None else str(row["old"])
        new_no = "" if row["new"] is None else str(row["new"])
        out.append(
            f'<div class="diff-row diff-{kind}">'
            f'<span class="diff-lineno diff-old">{old_no}</span>'
            f'<span class="diff-lineno diff-new">{new_no}</span>'
            f'<span class="diff-marker">{marker}</span>'
            f'<span class="diff-text">{html.escape(row["text"])}</span>'
            "</div>"
        )
    return "".join(out)


def format_diff(file_path: str, old: str, new: str, *, is_new_file: bool = False) -> str:
    """Render an Edit/Write as a unified diff block (UMBAU §5).

    ``is_new_file`` renders every line as an addition (Write). Otherwise a
    line-level unified diff between ``old`` and ``new`` is shown with +/-
    gutters and collapsed unchanged runs.
    """
    if is_new_file:
        new_lines = (new or "").splitlines()
        rows = [
            {"kind": "add", "old": None, "new": idx + 1, "text": line}
            for idx, line in enumerate(new_lines)
        ]
        additions, deletions = len(new_lines), 0
    else:
        rows, additions, deletions = _diff_rows(old or "", new or "")

    header = (
        f'<div class="diff-header">'
        f'<span class="diff-file">{html.escape(file_path or "(file)")}</span>'
        f'<span class="diff-stat diff-stat-add">+{additions}</span>'
        f'<span class="diff-stat diff-stat-del">−{deletions}</span>'
        "</div>"
    )
    return (
        '<div class="diff-block">'
        + header
        + '<div class="diff-body">'
        + _render_diff_rows(rows)
        + "</div></div>"
    )


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    if not text:
        return ""
    return re.compile(r"\x1B(?:[@-Z\\-_]|[0-?]*[ -/]*[@-~])").sub("", text)


def format_thinking(text: str) -> str:
    """Format <thinking> blocks as collapsible details (SpecStory Style)."""
    return f"""
    <details class="action-block group">
        <summary>
            <svg class="action-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m9 18 6-6-6-6"/></svg>
            <span class="thinking-label">Thinking</span>
            <span class="thinking-summary">{html.escape(text[:80]).strip()}{"..." if len(text) > 80 else ""}</span>
        </summary>
        <div class="action-content">
            <div class="thinking-content">{html.escape(text)}</div>
        </div>
    </details>
    """


def _normalize_tool_name(raw_name: str) -> str:
    name = (raw_name or "tool").strip().replace("_", " ")
    return re.sub(r"\s+", " ", name)


def _tool_icon(tool_name: str) -> str:
    lowered = tool_name.lower()
    if "bash" in lowered or "shell" in lowered or "terminal" in lowered:
        return "⚡"
    if "read" in lowered:
        return "📄"
    if "write" in lowered or "edit" in lowered:
        return "🛠"
    if "search" in lowered or "grep" in lowered or "glob" in lowered:
        return "🔎"
    if "web" in lowered or "fetch" in lowered or "http" in lowered:
        return "🌐"
    return "🔧"


def _guess_language(tool_name: str, text: str) -> str:
    lowered_tool = tool_name.lower()
    lowered_text = text.lower()
    stripped_text = (text or "").lstrip()
    if stripped_text.startswith("*** Begin Patch") or "@@" in text or "diff --git" in lowered_text:
        return "diff"
    if "bash" in lowered_tool or "shell" in lowered_tool:
        return "bash"
    if "python" in lowered_tool or "python" in lowered_text:
        return "python"
    if "json" in lowered_tool or lowered_text.strip().startswith("{"):
        return "json"
    if "sql" in lowered_text or "sqlite" in lowered_text:
        return "sql"
    return "plaintext"


def _safe_preview(value: str, limit: int = 140) -> str:
    preview = re.sub(r"\s+", " ", value or "").strip()
    if len(preview) <= limit:
        return preview
    return preview[: limit - 3] + "..."


def _format_shell_command_for_display(command_text: str) -> str:
    command = (command_text or "").strip()
    if not command or "\n" in command or len(command) < 80:
        return command

    wrapped = re.sub(r"\s*(&&|\|\||\|)\s*", r" \1\n", command)
    wrapped = re.sub(r";\s+", ";\n", wrapped)
    wrapped = re.sub(r"[ \t]{2,}", " ", wrapped)
    return wrapped.strip()


def _tool_importance(tool_name: str) -> str:
    lowered = (tool_name or "").lower()
    important_tokens = (
        "bash",
        "shell",
        "write",
        "edit",
        "apply_patch",
        "deploy",
        "commit",
        "delete",
        "migrate",
        "sql",
    )
    if any(token in lowered for token in important_tokens):
        return "major"
    return "minor"


def _render_kv_grid(tool_input) -> str:
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    items = []
    for key, value in list(tool_input.items())[:8]:
        if isinstance(value, (dict, list)):
            value_display = json.dumps(value, ensure_ascii=False)
        else:
            value_display = str(value)
        items.append(
            '<div class="action-kv-item">'
            f'<div class="action-kv-key">{html.escape(str(key))}</div>'
            f'<div class="action-kv-value">{html.escape(_safe_preview(value_display, 220))}</div>'
            "</div>"
        )
    if not items:
        return ""
    return '<div class="action-kv-grid">' + "".join(items) + "</div>"


def format_tool_result(text: str) -> str:
    text = (text or "").strip()
    if not text:
        text = "(empty output)"

    lowered = text.lower()
    lines = text.split("\n")
    line_count = len(lines)
    char_count = len(text)

    status = "neutral"
    if any(token in lowered for token in ("error", "exception", "traceback", "failed")):
        status = "error"
    elif any(token in lowered for token in ("ok", "success", "completed", "created", "updated")):
        status = "ok"

    first_line = next((line.strip() for line in lines if line.strip()), "Output")
    summary = _safe_preview(first_line, 90)

    exit_code = None
    exit_match = re.search(r"(?:exit\s*code|code)\s*[:=]?\s*(\d{1,3})", lowered)
    if exit_match:
        try:
            exit_code = int(exit_match.group(1))
        except ValueError:
            exit_code = None

    warning_count = len(re.findall(r"\bwarning\b", lowered))
    error_count = len(re.findall(r"\berror\b|\bexception\b|\btraceback\b", lowered))

    additions = 0
    deletions = 0
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1

    status_label = {
        "ok": "Success",
        "error": "Error",
        "neutral": "Result",
    }[status]

    chips = [
        f'<span class="action-meta">{line_count} lines</span>',
        f'<span class="action-meta">{char_count} chars</span>',
    ]
    if exit_code is not None:
        chips.append(f'<span class="action-meta">exit {exit_code}</span>')
    if warning_count > 0:
        chips.append(f'<span class="action-meta">warnings {warning_count}</span>')
    if error_count > 0:
        chips.append(f'<span class="action-meta">errors {error_count}</span>')
    if additions > 0 or deletions > 0:
        chips.append(f'<span class="action-meta">diff +{additions}/-{deletions}</span>')

    importance = (
        "major" if status == "error" or (exit_code is not None and exit_code != 0) else "minor"
    )

    return f"""
    <details class="action-block tool-result importance-{importance} group">
        <summary class="tool-call-pill tool-result-pill status-{status}">
            <svg class="action-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m9 18 6-6-6-6"/></svg>
            <span class="tool-chip status-{status}">{status_label}</span>
            <span class="action-title truncate">Tool result</span>
            <span class="action-meta-group">{"".join(chips)}</span>
            <span class="truncate opacity-75">{html.escape(summary)}</span>
        </summary>
        <div class="action-content">
            <pre class="action-output"><code class="language-plaintext">{html.escape(text)}</code></pre>
        </div>
    </details>
    """


def format_tool_display(tool_name: str, args: str) -> str:
    normalized_tool = _normalize_tool_name(tool_name)
    icon = _tool_icon(normalized_tool)
    importance = _tool_importance(normalized_tool)

    raw_args = (args or "").strip()
    arg_display = raw_args or "(no arguments)"
    preview = _safe_preview(arg_display, 88)

    parsed_input = None
    if raw_args.startswith("{") or raw_args.startswith("["):
        try:
            parsed_input = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_input = None

    if parsed_input is not None:
        arg_display = json.dumps(parsed_input, indent=2, ensure_ascii=False)

    command_text = ""
    if isinstance(parsed_input, dict):
        command_value = parsed_input.get("command") or parsed_input.get("cmd")
        if isinstance(command_value, str):
            command_text = command_value.strip()
    if not command_text and "bash" in normalized_tool.lower() and raw_args:
        command_text = raw_args

    language = _guess_language(normalized_tool, command_text or arg_display)
    display_command = command_text
    if language == "bash":
        display_command = _format_shell_command_for_display(command_text)

    # Edit/Write: show a real diff instead of a key/value dump — it is the whole
    # point of the call (UMBAU §5). The kv-grid is suppressed when a diff renders.
    diff_block = ""
    if isinstance(parsed_input, dict):
        lowered_tool = normalized_tool.lower().replace(" ", "")
        file_path = str(parsed_input.get("file_path", ""))
        if "edit" in lowered_tool and "old_string" in parsed_input:
            diff_block = format_diff(
                file_path,
                str(parsed_input.get("old_string", "")),
                str(parsed_input.get("new_string", "")),
            )
        elif lowered_tool == "write" and "content" in parsed_input:
            diff_block = format_diff(
                file_path, "", str(parsed_input.get("content", "")), is_new_file=True
            )

    kv_grid = "" if diff_block else _render_kv_grid(parsed_input)

    command_block = ""
    if display_command:
        command_block = (
            '<div class="action-section-title">Command</div>'
            f'<pre class="action-code"><code class="language-{language}">{html.escape(display_command)}</code></pre>'
        )

    raw_block = (
        '<details class="action-raw">'
        "<summary>Raw arguments</summary>"
        f'<pre class="action-output"><code class="language-{language}">{html.escape(arg_display)}</code></pre>'
        "</details>"
    )

    return f"""
    <details class="action-block tool-action importance-{importance} group">
        <summary class="tool-call-pill">
            <svg class="action-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m9 18 6-6-6-6"/></svg>
            <span class="tool-call-icon">{icon}</span>
            <span class="action-title">Run {html.escape(normalized_tool)}</span>
            <span class="action-meta">{len(arg_display)} chars</span>
            <span class="truncate opacity-75">{html.escape(preview)}</span>
        </summary>
        <div class="action-content">
            {command_block}
            {diff_block}
            {kv_grid}
            {raw_block}
        </div>
    </details>
    """


def decode_hex_or_str(value: Any) -> str:
    """Decode a SQLite blob that might be hex-encoded text or a plain string.

    Heuristic (matches the original CursorExtractor._decode_blob): values
    longer than 10 chars with no spaces in the first 50 chars and only hex
    digits in the first 100 are treated as hex and decoded as UTF-8.
    Anything else is returned unchanged; non-strings become "".

    Shared primitive (#120) — future SQLite-backed extractors should use
    this instead of re-deriving blob decoding per tool.
    """
    if not isinstance(value, str):
        return ""

    if (
        len(value) > 10
        and " " not in value[:50]
        and all(c in "0123456789abcdefABCDEF" for c in value[:100])
    ):
        try:
            return bytes.fromhex(value).decode("utf-8", errors="ignore")
        except ValueError:
            pass
    return value
