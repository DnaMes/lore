import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ...core.models import UnifiedSession

from ...utils.paths import lore_home
from ..base import LLMProvider, prepare_prompt


class StatsGenerator:
    def __init__(self, provider: Optional[LLMProvider] = None, output_dir: Optional[Path] = None):
        self.provider = provider
        self.redactions = 0  # secret-like strings removed from prompts sent so far
        self.output_dir = output_dir or lore_home() / "stats"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_session_stats(
        self,
        sessions: List["UnifiedSession"],
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        stats = self._compute_basic_stats(sessions)

        if use_llm and self.provider and self.provider.is_available():
            insights = self._generate_llm_insights(stats, sessions)
            stats["insights"] = insights

        return stats

    def _compute_basic_stats(self, sessions: List["UnifiedSession"]) -> Dict[str, Any]:
        if not sessions:
            return {"total_sessions": 0}

        tools = Counter(s.tool.value for s in sessions)
        projects = Counter(s.project_path or "Unknown" for s in sessions)
        dates = [s.created_at.date() for s in sessions]
        message_counts = [s.message_count for s in sessions]

        daily_counts = Counter(dates)
        recent_7_days = sum(
            count
            for date, count in daily_counts.items()
            if (datetime.now().date() - date).days <= 7
        )
        recent_30_days = sum(
            count
            for date, count in daily_counts.items()
            if (datetime.now().date() - date).days <= 30
        )

        total_messages = sum(message_counts)
        avg_messages = total_messages / len(sessions) if sessions else 0

        return {
            "total_sessions": len(sessions),
            "total_messages": total_messages,
            "avg_messages_per_session": round(avg_messages, 1),
            "sessions_last_7_days": recent_7_days,
            "sessions_last_30_days": recent_30_days,
            "by_tool": dict(tools.most_common(10)),
            "by_project": dict(projects.most_common(10)),
            "daily_activity": {
                str(date): count for date, count in sorted(daily_counts.items())[-30:]
            },
            "date_range": {
                "earliest": str(min(dates)),
                "latest": str(max(dates)),
            },
        }

    def _generate_llm_insights(
        self,
        stats: Dict[str, Any],
        sessions: List["UnifiedSession"],
    ) -> Dict[str, Any]:
        recent_sessions = sorted(sessions, key=lambda s: s.created_at, reverse=True)[:10]
        session_summaries = [
            {
                "tool": s.tool.value,
                "project": s.project_path or "Unknown",
                "messages": s.message_count,
                "date": str(s.created_at.date()),
                "title": s.title or "Untitled",
            }
            for s in recent_sessions
        ]

        prompt = f"""Analyze these AI coding session statistics and provide insights.

Statistics:
{json.dumps(stats, indent=2)}

Recent Sessions:
{json.dumps(session_summaries, indent=2)}

Provide:
1. Usage patterns (what tools/projects are most used and why)
2. Productivity insights (peak times, session lengths)
3. Recommendations for improvement
4. Notable trends

Format as JSON with keys: patterns, productivity, recommendations, trends"""

        if not self.provider:
            return {
                "patterns": "No LLM provider configured",
                "productivity": "",
                "recommendations": [],
                "trends": [],
            }

        try:
            # Secrets never leave the machine in the prompt (docs/PRIVACY.md):
            # redact the FINISHED prompt — stats JSON (project paths) and the
            # session summaries (titles) are session-derived too.
            prompt, redactions = prepare_prompt(prompt)
            self.redactions += redactions
            response = self.provider.generate(prompt, temperature=0.3)
            content = response.content.strip()

            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            return json.loads(content)

        except Exception:
            return {
                "patterns": "Unable to generate insights",
                "productivity": "",
                "recommendations": [],
                "trends": [],
            }

    def save_stats(self, stats: Dict[str, Any], filename: str = "stats.json") -> Path:
        output_path = self.output_dir / filename
        with open(output_path, "w") as f:
            json.dump(stats, f, indent=2, default=str)
        return output_path
