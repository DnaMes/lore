import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ...core.models import UnifiedSession

from ...services.hydration import REASON_EMPTY
from ...services.summary_input import compress, from_unified
from ...utils.paths import lore_home
from ..base import LLMProvider, prepare_prompt, transcript_safe

# A formatted session's summary is one call: the compressed conversation is
# bounded, and a session beyond this keeps its head and tail with a stated gap.
_FORMAT_SUMMARY_MAX_CHARS = 24_000
_FORMAT_SUMMARY_TAIL_CHARS = 6_000


# Why the AI half of the formatted page is empty. "ok" means the summary ran;
# any other value is a hydration reason (#177), or "provider-error" when the
# archived body was there but the call failed.
SUMMARY_OK = "ok"
SUMMARY_PROVIDER_ERROR = "provider-error"


@dataclass
class FormattedSession:
    session_id: str
    formatted_content: str
    summary: str
    tags: List[str]
    # The AI summary is optional; when it is missing this says why (#177).
    summary_status: str = SUMMARY_OK
    # Archived revision the summary was generated from, when it ran.
    source_revision: str = ""


def summary_transcript(messages: List) -> str:
    """The archived conversation as one prompt block, bounded with a visible gap.

    ``compress`` elides tool output and head/tail-elides very long messages; user
    and assistant text stays readable, so a decision made near the end of a long
    session is still in the summary prompt (#177).
    """
    text = compress(from_unified(messages)).text
    if len(text) <= _FORMAT_SUMMARY_MAX_CHARS:
        return text
    keep_head = _FORMAT_SUMMARY_MAX_CHARS - _FORMAT_SUMMARY_TAIL_CHARS
    head, tail = text[:keep_head], text[-_FORMAT_SUMMARY_TAIL_CHARS:]
    omitted = len(text) - keep_head - _FORMAT_SUMMARY_TAIL_CHARS
    return f"{head}\n[{omitted} characters omitted from the middle of the session]\n{tail}"


class SessionFormatter:
    def __init__(self, provider: LLMProvider, output_dir: Optional[Path] = None):
        self.provider = transcript_safe(provider)
        self.redactions = 0  # secret-like strings removed from prompts sent so far
        self.output_dir = output_dir or lore_home() / "formatted"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def format_session(
        self,
        session: "UnifiedSession",
        max_messages: int = 30,
        include_summary: bool = True,
        *,
        source_revision: str = "",
    ) -> FormattedSession:
        """Render one session; ``source_revision`` is the hydrated body it came from."""
        messages = session.messages[:max_messages]

        formatted_parts = []
        formatted_parts.append(f"# {session.title or 'Untitled Session'}\n")
        formatted_parts.append(f"**Tool:** {session.tool.value}")
        formatted_parts.append(f"**Project:** {session.project_path or 'Unknown'}")
        formatted_parts.append(f"**Date:** {session.created_at.strftime('%Y-%m-%d %H:%M')}")
        formatted_parts.append(f"**Messages:** {session.message_count}\n")
        formatted_parts.append("---\n")

        for msg in messages:
            role = msg.role.value.upper()
            content = self._clean_content(msg.content or "")

            if role == "USER":
                formatted_parts.append(f"### 👤 User\n{content}\n")
            elif role == "ASSISTANT":
                formatted_parts.append(f"### 🤖 Assistant\n{content}\n")
            else:
                formatted_parts.append(f"### {role}\n{content}\n")

        formatted_content = "\n".join(formatted_parts)

        summary = ""
        tags = []
        status = SUMMARY_OK
        if include_summary and self.provider.is_available():
            if not session.messages:
                # No archived body reached this task: say so instead of asking a
                # provider to summarise an empty reconstruction (#177).
                status = REASON_EMPTY
            else:
                summary, tags, status = self._generate_summary_and_tags(session)

        return FormattedSession(
            session_id=session.session_id,
            formatted_content=formatted_content,
            summary=summary,
            tags=tags,
            summary_status=status,
            source_revision=source_revision,
        )

    def _clean_content(self, content: str) -> str:
        content = re.sub(r"\[Tool:.*?\]", "", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    def _generate_summary_and_tags(self, session: "UnifiedSession") -> tuple:
        """Summarise the canonical conversation, head/tail elided when huge (#177).

        Returns ``(summary, tags, status)``. The whole archived body is used, not
        the first few messages: a late correction is what the summary is for.
        """
        conversation_text = summary_transcript(session.messages)

        prompt = f"""Summarize this AI coding session and suggest tags.

Tool: {session.tool.value}
Project: {session.project_path or "Unknown"}

Conversation:
{conversation_text}

Provide:
1. A 2-3 sentence summary of what was accomplished
2. 3-5 relevant tags (technologies, concepts, patterns)

Format as JSON:
{{
  "summary": "...",
  "tags": ["...", "..."]
}}"""

        try:
            prompt, redactions = prepare_prompt(prompt)
            self.redactions += redactions
            response = self.provider.generate(prompt, temperature=0.3)
            content = response.content.strip()

            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
                if content.startswith("json"):
                    content = content[4:].strip()

            import json

            data = json.loads(content)
            return data.get("summary", ""), data.get("tags", []), SUMMARY_OK

        except Exception:
            return "", [], SUMMARY_PROVIDER_ERROR

    def save_formatted_session(
        self,
        formatted: FormattedSession,
        filename: Optional[str] = None,
    ) -> Path:
        if filename is None:
            filename = f"{formatted.session_id}.md"

        output_path = self.output_dir / filename

        content = formatted.formatted_content
        if formatted.summary:
            content += f"\n\n---\n\n## Summary\n{formatted.summary}\n"
            if formatted.source_revision:
                content += f"\n_Source revision: {formatted.source_revision[:12]}_\n"
        if formatted.tags:
            content += f"\n**Tags:** {', '.join(formatted.tags)}\n"
        if formatted.summary_status != SUMMARY_OK:
            # The AI half is optional; the page says why it is missing (#177).
            content += (
                f"\n_AI summary unavailable ({formatted.summary_status}); "
                "the session body above is complete._\n"
            )

        with open(output_path, "w") as f:
            f.write(content)

        return output_path
