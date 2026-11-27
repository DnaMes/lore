"""Native Claude Code CLI adapter (#182).

Invocation is the documented headless interface of the Claude Code CLI:

* ``claude --version`` — installed version (parsed, reported, never guessed).
* ``claude --print`` — non-interactive run; with no prompt argument the prompt is
  read from standard input, so transcript text never appears in the argument
  vector and stays out of the process list.
* ``claude --print --output-format json`` — one JSON document on stdout:
  ``{"type": "result", "subtype": "success", "is_error": false, "result": …,
  "session_id": …, "total_cost_usd": …}``. ``is_error: true`` is a failed
  result, reported as an execution or auth failure, never parsed as an answer.
* ``--model`` — passed only when the request names a model; otherwise the CLI's
  own configured model answers (documented aliases such as ``sonnet`` are the
  CLI's vocabulary, not Lore's).

``claude --help`` must document ``--print`` and ``--output-format``, otherwise the
installed CLI is reported unavailable with that reason instead of being invoked
on a guess.

Deliberately unsupported: ``--continue``/``--resume`` (Lore does not adopt the
user's interactive sessions), tool or MCP permission changes, ``claude login`` on
the user's behalf, and any credential file access. The adapter strips API-key
variables from the child environment so only the CLI's own native login can
answer.
"""

from __future__ import annotations

from typing import List

from .contract import AdapterResult
from .errors import AdapterAuthError, AdapterExecutionError, AdapterOutputError
from .native_cli import AUTH_FAILURE_MARKERS, NativeCliAdapter, parse_json_document


class ClaudeCliAdapter(NativeCliAdapter):
    """Analyse a session through the installed Claude Code CLI (`claude --print`)."""

    PROVIDER = "claude-cli"
    EXECUTABLE = "claude"
    REQUIRED_HELP_TOKENS = ("--print", "--output-format")

    def build_argv(self, probe) -> List[str]:
        argv = [probe.path or self.EXECUTABLE, "--print", "--output-format", "json"]
        if self.request.model:
            argv += ["--model", self.request.model]
        return argv

    def parse_output(self, raw: str) -> AdapterResult:
        payload = parse_json_document(raw, self.PROVIDER, "claude --print --output-format json")
        structured = {
            "type": str(payload.get("type") or ""),
            "subtype": str(payload.get("subtype") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "num_turns": payload.get("num_turns")
            if isinstance(payload.get("num_turns"), int)
            else 0,
            "total_cost_usd": (
                payload.get("total_cost_usd")
                if isinstance(payload.get("total_cost_usd"), (int, float))
                else None
            ),
        }
        if payload.get("is_error"):
            detail = str(payload.get("result") or payload.get("error") or "no detail")
            if any(marker in detail.lower() for marker in AUTH_FAILURE_MARKERS):
                raise AdapterAuthError(self.PROVIDER, detail[:400])
            raise AdapterExecutionError(self.PROVIDER, 0, detail[:400])
        content = payload.get("result")
        if not isinstance(content, str) or not content.strip():
            raise AdapterOutputError(self.PROVIDER, "claude --print report has no 'result' text")
        usage = payload.get("usage")
        tokens = 0
        if isinstance(usage, dict):
            for key in ("output_tokens", "input_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens += value
        return AdapterResult(
            content=content.strip(),
            model=self.request.model or "cli-default",
            structured=structured,
            tokens_used=tokens,
        )
