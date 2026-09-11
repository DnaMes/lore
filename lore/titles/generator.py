import json
import logging
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..core.models import UnifiedSession

yake: Any = None
ollama: Any = None

try:
    import yake

    YAKE_AVAILABLE = True
except ImportError:
    yake = None
    YAKE_AVAILABLE = False

try:
    import ollama

    OLLAMA_AVAILABLE = True
except ImportError:
    ollama = None
    OLLAMA_AVAILABLE = False

# Check if Gemini CLI is available
import shutil

from ..utils.paths import lore_home

logger = logging.getLogger(__name__)

GEMINI_CLI_AVAILABLE = shutil.which("gemini") is not None


class TitleStrategy(Enum):
    FAST = "fast"
    KEYWORD = "keyword"
    SMART = "smart"
    AUTO = "auto"


class TitleGenerator:
    def __init__(
        self,
        strategy: TitleStrategy = TitleStrategy.AUTO,
        cache_dir: Optional[Path] = None,
        ollama_model: str = "phi3:mini",
    ):
        self.strategy = strategy
        self.cache_dir = cache_dir or lore_home() / "title_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "titles.json"
        self.ollama_model = ollama_model
        self._cache = self._load_cache()

        if YAKE_AVAILABLE and yake is not None:
            self._yake = yake.KeywordExtractor(lan="en", n=3, top=5, dedupLim=0.7)
        else:
            self._yake = None

    def generate(self, session: "UnifiedSession", force: bool = False) -> str:
        from ..core.models import TitleSource

        if (
            session.title
            and session.title.strip()
            and not force
            and not self._is_generic_title(session.title)
        ):
            session.title_source = TitleSource.NATIVE
            return session.title.strip()

        if (
            session.summary
            and session.summary.strip()
            and not force
            and not self._is_generic_title(session.summary)
        ):
            session.title_source = TitleSource.SUMMARY
            title = session.summary.strip()
            return title[:80] if len(title) > 80 else title

        if not force:
            cached = self._get_cached(session.session_id)
            if cached:
                return cached

        title = self._generate_by_strategy(session)
        self._cache_title(session.session_id, title)

        return title

    def _generate_by_strategy(self, session: "UnifiedSession") -> str:
        from ..core.models import TitleSource

        if self.strategy == TitleStrategy.SMART and OLLAMA_AVAILABLE:
            title = self._generate_llm(session)
            if title:
                session.title_source = TitleSource.LLM
                return title

        if self.strategy == TitleStrategy.KEYWORD and self._yake:
            title = self._generate_keywords(session)
            if title:
                session.title_source = TitleSource.KEYWORDS
                return title

        if self.strategy == TitleStrategy.AUTO:
            return self._generate_auto(session)

        return self._generate_first_message(session)

    def _generate_auto(self, session: "UnifiedSession") -> str:
        from ..core.models import TitleSource

        msg_count = session.message_count
        first_msg = self._get_first_user_message(session)
        first_msg_len = len(first_msg)

        if msg_count >= 10 and OLLAMA_AVAILABLE:
            title = self._generate_llm(session)
            if title:
                session.title_source = TitleSource.LLM
                return title

        if (msg_count >= 5 or first_msg_len >= 100) and self._yake:
            title = self._generate_keywords(session)
            if title:
                session.title_source = TitleSource.KEYWORDS
                return title

        return self._generate_first_message(session)

    def _generate_keywords(self, session: "UnifiedSession") -> Optional[str]:
        if not self._yake:
            return None

        text = self._get_first_user_message(session)
        if not text or len(text) < 20:
            return None

        keywords = self._yake.extract_keywords(text)
        top_keywords = [kw for kw, score in keywords[:3]]

        if top_keywords:
            title = " • ".join(top_keywords)
            return title[:80] if len(title) > 80 else title

        return None

    def _generate_llm(self, session: "UnifiedSession") -> Optional[str]:
        if GEMINI_CLI_AVAILABLE:
            return self._generate_llm_gemini(session)
        elif OLLAMA_AVAILABLE:
            return self._generate_llm_ollama(session)
        return None

    def _generate_llm_gemini(self, session: "UnifiedSession") -> Optional[str]:
        import subprocess

        context = self._build_context(session, max_messages=6)
        prompt = f"""Generate a concise title (max 8 words) for this AI coding session.

Tool: {session.tool.value}
Project: {session.project_path or "Unknown"}

Conversation:
{context}

Respond with ONLY the title, no quotes, no explanation."""

        try:
            result = subprocess.run(["gemini", prompt], capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                title = result.stdout.strip()
                title = title.replace('"', "").replace("'", "").strip()
                return title[:80] if len(title) > 80 else title

        except Exception as exc:
            # The gemini CLI is an optional best-effort path — but a broken
            # install must still leave a signal (#121).
            logger.debug("gemini CLI title generation failed: %s", exc)

        return None

    def _generate_llm_ollama(self, session: "UnifiedSession") -> Optional[str]:
        if not OLLAMA_AVAILABLE or ollama is None:
            return None

        context = self._build_context(session, max_messages=6)
        prompt = f"""Generate a concise title (max 8 words) for this AI coding session.

Tool: {session.tool.value}
Project: {session.project_path or "Unknown"}

Conversation:
{context}

Title:"""

        try:
            client = ollama.Client()
            response = client.chat(
                model=self.ollama_model,
                messages=[{"role": "user", "content": prompt}],
                options={"num_predict": 30, "temperature": 0.3},
            )

            title = response["message"]["content"].strip()
            title = title.replace('"', "").replace("'", "").strip()
            return title[:80] if len(title) > 80 else title

        except Exception:
            return None

    def _generate_first_message(self, session: "UnifiedSession") -> str:
        from ..core.models import TitleSource

        text = self._get_first_user_message(session)

        if text:
            session.title_source = TitleSource.FIRST_MESSAGE
            words = text.split()[:10]
            title = " ".join(words)
            if len(title) > 80:
                title = title[:77] + "..."
            return title

        session.title_source = TitleSource.FALLBACK
        date = session.created_at.strftime("%Y-%m-%d")
        tool = session.tool.value.replace("-", " ").title()
        return f"{tool} Session {date}"

    def _get_first_user_message(self, session: "UnifiedSession") -> str:
        from ..core.models import Role

        for msg in session.messages:
            if msg.role == Role.USER:
                clean = re.sub(r"\[Tool:.*?\]", "", msg.content)
                clean = re.sub(r"```.*?```", "", clean, flags=re.DOTALL)
                clean = clean.strip()
                if clean and not self._is_placeholder_message(clean):
                    return clean
        return ""

    def _is_placeholder_message(self, text: str) -> bool:
        lowered = text.lower().strip()
        patterns = [
            "caveat: the messages below were generated by the user",
            "do not respond to these messages",
            "messages below were generated by the user while running local commands",
            "system prompt",
            "conversation started",
        ]
        return any(p in lowered for p in patterns)

    def _is_generic_title(self, text: str) -> bool:
        lowered = text.lower().strip()
        if not lowered:
            return True
        generic_prefixes = [
            "caveat: the messages below were generated by the user",
            "claude code session",
            "gemini cli session",
            "codex session",
            "session ",
        ]
        return any(lowered.startswith(p) for p in generic_prefixes)

    def _build_context(self, session: "UnifiedSession", max_messages: int = 6) -> str:
        parts = []
        for msg in session.messages[:max_messages]:
            role = msg.role.value.upper()
            content = (msg.content or "")[:200]
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    def _load_cache(self) -> dict:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _get_cached(self, session_id: str) -> Optional[str]:
        entry = self._cache.get(session_id)
        if entry:
            try:
                cache_time = datetime.fromisoformat(entry["timestamp"])
                age_days = (datetime.now() - cache_time).days
                if age_days < 30 and not self._is_generic_title(entry.get("title", "")):
                    return entry["title"]
            except (KeyError, ValueError):
                pass
        return None

    def _cache_title(self, session_id: str, title: str):
        self._cache[session_id] = {
            "title": title,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except OSError:
            pass
