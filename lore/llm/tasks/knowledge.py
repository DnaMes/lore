import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ...core.models import UnifiedSession

from ...utils.paths import lore_home
from ..base import LLMProvider


@dataclass
class KnowledgeEntry:
    session_id: str
    topic: str
    key_points: List[str]
    code_snippets: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    project: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "topic": self.topic,
            "key_points": self.key_points,
            "code_snippets": self.code_snippets,
            "decisions": self.decisions,
            "tools_used": self.tools_used,
            "project": self.project,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEntry":
        """Rebuild an entry from :meth:`to_dict` output; missing keys take defaults."""
        try:
            timestamp = datetime.fromisoformat(str(data.get("timestamp")))
        except ValueError:
            timestamp = datetime.now()

        def _strings(key: str) -> List[str]:
            value = data.get(key)
            return [str(i) for i in value] if isinstance(value, list) else []

        return cls(
            session_id=str(data.get("session_id", "")),
            topic=str(data.get("topic") or "Unknown"),
            key_points=_strings("key_points"),
            code_snippets=_strings("code_snippets"),
            decisions=_strings("decisions"),
            tools_used=_strings("tools_used"),
            project=data.get("project"),
            timestamp=timestamp,
        )


class KnowledgeExtractor:
    def __init__(self, provider: LLMProvider, output_dir: Optional[Path] = None):
        self.provider = provider
        self.output_dir = output_dir or lore_home() / "knowledge"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def conversation_text(session: "UnifiedSession", max_messages: int = 20) -> str:
        """Return the exact conversation text placed in the extraction prompt."""
        return "\n".join(
            f"{msg.role.value.upper()}: {(msg.content or '')[:500]}"
            for msg in session.messages[:max_messages]
        )

    @staticmethod
    def build_prompt(session: "UnifiedSession", max_messages: int = 20) -> str:
        """Return the full extraction prompt, the input a stored result is keyed on."""
        conversation_text = KnowledgeExtractor.conversation_text(session, max_messages)
        return f"""Extract structured knowledge from this AI coding session.

Tool: {session.tool.value}
Project: {session.project_path or "Unknown"}
Date: {session.created_at.date()}

Conversation:
{conversation_text}

Extract:
1. Main topic/goal (one sentence)
2. Key points learned (3-5 bullet points)
3. Code snippets (important code shown, max 3)
4. Decisions made (architectural, design choices)
5. Tools/technologies mentioned

Format as JSON:
{{
  "topic": "...",
  "key_points": ["...", "..."],
  "code_snippets": ["...", "..."],
  "decisions": ["...", "..."],
  "tools_used": ["...", "..."]
}}"""

    def extract_from_session(
        self,
        session: "UnifiedSession",
        max_messages: int = 20,
    ) -> Optional[KnowledgeEntry]:
        if not self.provider.is_available():
            return None

        prompt = self.build_prompt(session, max_messages)

        try:
            response = self.provider.generate(prompt, temperature=0.2)
            content = response.content.strip()

            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
                if content.startswith("json"):
                    content = content[4:].strip()

            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            topic = data.get("topic", "Unknown")
            fields = {
                name: data.get(name) or []
                for name in ("key_points", "code_snippets", "decisions", "tools_used")
            }
            # A malformed response must count as a failed call, never be persisted:
            # a stored row with the wrong shape would break every later run.
            if not isinstance(topic, str) or not all(
                isinstance(v, list) and all(isinstance(i, str) for i in v) for v in fields.values()
            ):
                return None

            return KnowledgeEntry(
                session_id=session.session_id,
                topic=topic,
                key_points=fields["key_points"],
                code_snippets=fields["code_snippets"],
                decisions=fields["decisions"],
                tools_used=fields["tools_used"],
                project=session.project_path,
            )

        except Exception:
            return None

    def extract_from_sessions(
        self,
        sessions: List["UnifiedSession"],
        limit: int = 50,
    ) -> List[KnowledgeEntry]:
        entries = []
        for session in sessions[:limit]:
            entry = self.extract_from_session(session)
            if entry:
                entries.append(entry)
        return entries

    def build_knowledge_base(
        self,
        entries: List[KnowledgeEntry],
        filename: str = "knowledge_base.json",
    ) -> Path:
        knowledge_base = {
            "version": "1.0",
            "created": datetime.now().isoformat(),
            "total_entries": len(entries),
            "entries": [e.to_dict() for e in entries],
            "by_topic": self._group_by_topic(entries),
            "by_project": self._group_by_project(entries),
            "by_tool": self._group_by_tool(entries),
        }

        output_path = self.output_dir / filename
        with open(output_path, "w") as f:
            json.dump(knowledge_base, f, indent=2)

        return output_path

    def _group_by_topic(self, entries: List[KnowledgeEntry]) -> Dict[str, List[str]]:
        topics = {}
        for entry in entries:
            topic_key = entry.topic.lower()[:50]
            if topic_key not in topics:
                topics[topic_key] = []
            topics[topic_key].append(entry.session_id)
        return topics

    def _group_by_project(self, entries: List[KnowledgeEntry]) -> Dict[str, List[str]]:
        projects = {}
        for entry in entries:
            project = entry.project or "Unknown"
            if project not in projects:
                projects[project] = []
            projects[project].append(entry.session_id)
        return projects

    def _group_by_tool(self, entries: List[KnowledgeEntry]) -> Dict[str, List[str]]:
        tools = {}
        for entry in entries:
            for tool in entry.tools_used:
                if tool not in tools:
                    tools[tool] = []
                tools[tool].append(entry.session_id)
        return tools
