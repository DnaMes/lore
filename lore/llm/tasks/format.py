import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ...core.models import UnifiedSession

from ...utils.paths import lore_home
from ..base import LLMProvider, prepare_prompt, transcript_safe


@dataclass
class FormattedSession:
    session_id: str
    formatted_content: str
    summary: str
    tags: List[str]


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
    ) -> FormattedSession:
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

        if include_summary and self.provider.is_available():
            summary, tags = self._generate_summary_and_tags(session, messages[:10])

        return FormattedSession(
            session_id=session.session_id,
            formatted_content=formatted_content,
            summary=summary,
            tags=tags,
        )

    def _clean_content(self, content: str) -> str:
        content = re.sub(r"\[Tool:.*?\]", "", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    def _generate_summary_and_tags(
        self,
        session: "UnifiedSession",
        messages: List,
    ) -> tuple:
        conversation = []
        for msg in messages[:10]:
            role = msg.role.value.upper()
            content = (msg.content or "")[:300]
            conversation.append(f"{role}: {content}")

        conversation_text = "\n".join(conversation)

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
            return data.get("summary", ""), data.get("tags", [])

        except Exception:
            return "", []

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
        if formatted.tags:
            content += f"\n**Tags:** {', '.join(formatted.tags)}\n"

        with open(output_path, "w") as f:
            f.write(content)

        return output_path
