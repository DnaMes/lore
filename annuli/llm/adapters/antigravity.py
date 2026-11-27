"""Antigravity CLI adapter — contract note plus an availability-gated stub (#183).

Availability is always false today, with a reason. Nothing in this module guesses
a flag, a subcommand or an output shape, because the supported contract could not
be confirmed offline: issue #183 says "confirm the supported CLI contract before
coding; never infer it from the IDE data directory".

Open questions that block a real adapter:

1. Is there a non-interactive *CLI* for Antigravity, as opposed to the IDE / agent
   manager and the `brain/` data directory Lore already extracts from? Which
   executable name and installation channel is supported?
2. What is the documented non-interactive entry point — prompt on stdin, a
   ``-p``/``--print`` flag, a subcommand — and is it stable?
3. Which structured-output mode is documented (JSON document? JSONL events?),
   and under which flag?
4. How do subscription credits apply to a non-interactive run, and what does an
   exhausted-credit failure look like in exit code and output?
5. Which version range can be pinned for a capability probe, and does
   ``--help`` list the flags so an installed CLI can be checked rather than
   assumed?

Until those are answered, an Antigravity conversation stays an archived source
artifact (the extractor keeps it) and the analysis adapter reports unavailability.
The probe below only checks whether an ``antigravity`` executable exists, so the
reason distinguishes "not installed" from "installed but no confirmed contract".
"""

from __future__ import annotations

import shutil

from .contract import CliProbe
from .native_cli import NativeCliAdapter


class AntigravityCliAdapter(NativeCliAdapter):
    """Report unavailability; invoke nothing until the contract is confirmed (#183)."""

    PROVIDER = "antigravity-cli"
    EXECUTABLE = "antigravity"

    def _detect(self) -> CliProbe:
        path = shutil.which(self.EXECUTABLE)
        if path is None:
            return CliProbe(
                self.PROVIDER,
                self.EXECUTABLE,
                False,
                reason=f"no '{self.EXECUTABLE}' executable on PATH",
            )
        return CliProbe(
            self.PROVIDER,
            self.EXECUTABLE,
            False,
            path=path,
            reason=(
                "an 'antigravity' executable exists but no supported non-interactive "
                "CLI contract (prompt transport, structured output, credit handling) "
                "has been confirmed; see the module docstring of "
                "lore/llm/adapters/antigravity.py"
            ),
        )
