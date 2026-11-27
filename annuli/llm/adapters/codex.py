"""Native Codex CLI adapter (#181).

Invocation is the documented non-interactive interface of the OpenAI Codex CLI;
nothing here reads or writes Codex configuration:

* ``codex --version`` — installed version (parsed, reported, never guessed).
* ``codex exec -`` — non-interactive run; ``-`` reads the prompt from standard
  input, so transcript text never appears in the argument vector.
* ``codex exec --json`` — JSONL event stream on stdout. The answer is the last
  ``agent_message``, in the current shape
  ``{"type": "item.completed", "item": {"item_type": "agent_message", "text": …}}``
  or the older ``{"msg": {"type": "agent_message", "message": …}}``.
* ``--sandbox read-only`` and ``--skip-git-repo-check`` — the run may not modify
  the workspace and may start outside a git repository (Lore runs it from a
  temporary directory).

``codex exec --help`` must document ``--json``, otherwise the installed CLI is
reported unavailable with that reason rather than invoked on a guess.

Deliberately unsupported: the interactive TUI, image input, ``codex login`` on the
user's behalf, and any credential file access. Authentication is whatever
``codex login`` already installed; the adapter strips API-key variables from the
child environment so a key Lore holds can never answer for Codex.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .contract import AdapterResult
from .errors import AdapterOutputError
from .native_cli import NativeCliAdapter


def _agent_message(event: Dict[str, Any]) -> Optional[str]:
    """The agent message carried by one documented event, if it carries one."""
    item = event.get("item")
    if isinstance(item, dict) and item.get("item_type") in ("agent_message", "assistant_message"):
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    message = event.get("msg")
    if isinstance(message, dict) and message.get("type") == "agent_message":
        text = message.get("message")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


class CodexCliAdapter(NativeCliAdapter):
    """Analyse a session through the installed Codex CLI (`codex exec --json`)."""

    PROVIDER = "codex-cli"
    EXECUTABLE = "codex"
    HELP_ARGV = ("exec",)
    REQUIRED_HELP_TOKENS = ("--json",)

    def build_argv(self, probe) -> List[str]:
        argv = [
            probe.path or self.EXECUTABLE,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        ]
        if self.request.model:
            argv += ["--model", self.request.model]
        # "-" is the documented "read the prompt from stdin" marker.
        argv.append("-")
        return argv

    def parse_output(self, raw: str) -> AdapterResult:
        events = 0
        partial = False
        content = ""
        final: Dict[str, Any] = {}
        for number, line in enumerate((raw or "").splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                if events:
                    # A truncated final line after usable events is partial
                    # output, not a failure: report it as such.
                    partial = True
                    break
                raise AdapterOutputError(
                    self.PROVIDER, f"codex exec --json line {number} is not JSON"
                ) from None
            if not isinstance(event, dict):
                raise AdapterOutputError(
                    self.PROVIDER, f"codex exec --json line {number} is not a JSON object"
                )
            events += 1
            message = _agent_message(event)
            if message is not None:
                content = message
                final = event
        if not content:
            raise AdapterOutputError(self.PROVIDER, "codex exec --json emitted no agent message")
        return AdapterResult(
            content=content,
            model=self.request.model or "cli-default",
            structured={
                "events": events,
                "final_event_type": str(final.get("type") or "agent_message"),
            },
            tokens_used=_usage_total(raw),
            partial=partial,
        )


def _usage_total(raw: str) -> int:
    """Sum of ``output_tokens`` reported in any ``turn.completed`` usage block."""
    total = 0
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if not isinstance(usage, dict):
            continue
        for key in ("output_tokens", "input_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
    return total
