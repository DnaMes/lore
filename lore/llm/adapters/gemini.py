"""Native Gemini CLI adapter (#184): the CLI answers, the API adapter never does.

This module and ``lore/llm/gemini.py`` are separate providers on purpose. The
``gemini`` provider talks to the Generative Language API with a Lore-configured
credential; this adapter runs the installed ``gemini`` executable with whatever
login that CLI already has and never touches an API client, an OAuth refresh
token, or ``~/.gemini/oauth_creds.json``. A missing CLI is an explicit
unavailable state — it is never answered by the API provider.

Invocation is the documented non-interactive interface:

* ``gemini --version`` — installed version (parsed, reported, never guessed).
* ``gemini -p "<instruction>"`` — non-interactive mode. The instruction on argv is
  a constant ("follow the instructions on stdin"); the prompt itself travels on
  standard input in this module too, so transcript text stays out of the process
  list (the same choice ``lore/titles/generator.py`` makes).
* ``gemini --output-format json`` — one JSON document on stdout with the answer in
  ``response`` and accounting under ``stats``. Any other shape is malformed
  output, not an answer.
* ``--model`` — passed only when the request names a model; otherwise the CLI's
  own configured model answers.

``gemini --help`` must document ``-p`` and ``--output-format``, otherwise the
installed CLI is reported unavailable with that reason instead of being invoked
on a guess.

Deliberately unsupported: interactive mode, extensions/tool configuration,
``gemini login`` on the user's behalf, reading or copying CLI credentials, and
non-JSON output modes.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .contract import AdapterResult
from .errors import AdapterOutputError
from .native_cli import NativeCliAdapter, parse_json_document

# Constant argument: the prompt itself is on stdin, never in argv.
STDIN_INSTRUCTION = "Follow the instructions in the input."


class GeminiCliAdapter(NativeCliAdapter):
    """Analyse a session through the installed Gemini CLI (`gemini -p`, JSON output)."""

    PROVIDER = "gemini-cli"
    EXECUTABLE = "gemini"
    REQUIRED_HELP_TOKENS = ("-p", "--output-format")

    def build_argv(self, probe) -> List[str]:
        argv = [
            probe.path or self.EXECUTABLE,
            "--output-format",
            "json",
            "-p",
            STDIN_INSTRUCTION,
        ]
        if self.request.model:
            argv += ["--model", self.request.model]
        return argv

    def parse_output(self, raw: str) -> AdapterResult:
        payload = parse_json_document(raw, self.PROVIDER, "gemini --output-format json")
        content = payload.get("response")
        if not isinstance(content, str) or not content.strip():
            raise AdapterOutputError(
                self.PROVIDER, "gemini --output-format json has no 'response' text"
            )
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        return AdapterResult(
            content=content.strip(),
            model=self.request.model or "cli-default",
            structured={"stats": stats},
            tokens_used=_tokens_from_stats(stats),
        )


def _tokens_from_stats(stats: Dict[str, Any]) -> int:
    """Total tokens from the CLI's per-model ``stats.models.*.tokens.total`` blocks."""
    total = 0
    models = stats.get("models")
    if not isinstance(models, dict):
        return total
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        value = tokens.get("total")
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total
