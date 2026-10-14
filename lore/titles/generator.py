import json
import logging
import os
import re
import tempfile
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

# Check if a cloud CLI is installed: only used to *report* availability —
# never to pick a provider implicitly (#185).
import shutil

from ..utils.paths import lore_home, restrict_file, secure_dir

logger = logging.getLogger(__name__)

GEMINI_CLI_AVAILABLE = shutil.which("gemini") is not None

# Providers a title request may be routed to (#185). Titles send a transcript
# excerpt, so the default is the local one: the Gemini CLI is cloud and must be
# selected explicitly, never picked because its binary happens to exist.
TITLE_PROVIDERS = ("local", "gemini-cli")

# Bumped when the title prompt changes; recorded with every generated title so a
# cached title can be recognised as pre-change output.
TITLE_PROMPT_VERSION = 1

# How much of the sent excerpt is recorded with the title (audit trail, not a copy).
TITLE_EXCERPT_MAX_CHARS = 120


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
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.strategy = strategy
        self.provider = provider or "local"
        if self.provider not in TITLE_PROVIDERS:
            raise ValueError(
                f"Unknown title provider {self.provider!r}; expected one of: "
                f"{', '.join(TITLE_PROVIDERS)}"
            )
        self.model = model or (ollama_model if self.provider == "local" else "gemini-cli")
        self.cache_dir = cache_dir or lore_home() / "title_cache"
        secure_dir(self.cache_dir)
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
            self._cache_title(
                session.session_id,
                session.title.strip(),
                {"provider": "none", "model": "", "status": "native", "excerpt": ""},
            )
            return session.title.strip()

        if (
            session.summary
            and session.summary.strip()
            and not force
            and not self._is_generic_title(session.summary)
        ):
            session.title_source = TitleSource.SUMMARY
            title = session.summary.strip()
            title = title[:80] if len(title) > 80 else title
            self._cache_title(
                session.session_id,
                title,
                {"provider": "none", "model": "", "status": "summary", "excerpt": ""},
            )
            return title

        if not force:
            cached = self._get_cached(session.session_id)
            if cached:
                return cached

        title, record = self._generate_by_strategy_recorded(session)
        self._cache_title(session.session_id, title, record)

        return title

    def _generate_by_strategy_recorded(self, session: "UnifiedSession") -> tuple[str, dict]:
        """Run the strategy chain and say how the title was produced (#197/#185).

        Returns ``(title, record)`` where ``record`` names the provider, model,
        prompt version, a bounded excerpt of what was sent, and the status — so
        a cached title can be traced back to the call that produced it.
        """
        from ..core.models import TitleSource

        if self.strategy == TitleStrategy.SMART and OLLAMA_AVAILABLE:
            title = self._generate_llm(session)
            if title:
                session.title_source = TitleSource.LLM
                return title, self._llm_record(session, "llm")

        if self.strategy == TitleStrategy.KEYWORD and self._yake:
            title = self._generate_keywords(session)
            if title:
                session.title_source = TitleSource.KEYWORDS
                return title, self._record("keywords")

        if self.strategy == TitleStrategy.AUTO:
            title, record = self._generate_auto_recorded(session)
            return title, record

        return self._generate_first_message(session), self._record("first_message")

    def _generate_auto_recorded(self, session: "UnifiedSession") -> tuple[str, dict]:
        from ..core.models import TitleSource

        msg_count = session.message_count
        first_msg = self._get_first_user_message(session)
        first_msg_len = len(first_msg)

        if msg_count >= 10 and OLLAMA_AVAILABLE:
            title = self._generate_llm(session)
            if title:
                session.title_source = TitleSource.LLM
                return title, self._llm_record(session, "llm")
            # The selected provider did not answer: fall back to the *local*
            # strategies only. Never another provider (#185) — that is how
            # transcript text used to reach a different tool than the one the
            # run selected.
            failed_record = self._llm_record(session, "llm-failed")
        else:
            failed_record = None

        if (msg_count >= 5 or first_msg_len >= 100) and self._yake:
            title = self._generate_keywords(session)
            if title:
                session.title_source = TitleSource.KEYWORDS
                record = self._record("keywords")
                if failed_record:
                    record["llm_failed"] = failed_record
                return title, record

        title = self._generate_first_message(session)
        record = self._record("first_message")
        if failed_record:
            record["llm_failed"] = failed_record
        return title, record

    def _record(
        self,
        status: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        excerpt: str = "",
        prompt_version: int = 0,
    ) -> dict:
        return {
            "provider": provider or "none",
            "model": model or "",
            "prompt_version": prompt_version,
            "excerpt": excerpt[:TITLE_EXCERPT_MAX_CHARS],
            "status": status,
        }

    def _llm_record(self, session: "UnifiedSession", status: str) -> dict:
        return self._record(
            status,
            provider=self.provider,
            model=self.model,
            prompt_version=TITLE_PROMPT_VERSION,
            excerpt=self._build_context(session, max_messages=6),
        )

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
        """Ask the *selected* provider only — never fall through to another one (#185)."""
        if self.provider == "gemini-cli":
            return self._generate_llm_gemini(session)
        if self.provider == "local":
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

    def _cache_title(self, session_id: str, title: str, record: Optional[dict] = None):
        entry = {
            "title": title,
            "timestamp": datetime.now().isoformat(),
        }
        if record:
            entry["record"] = record
        self._cache[session_id] = entry
        try:
            # Atomic and owner-only: the record can carry a transcript excerpt (#185).
            fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=".titles.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            os.replace(tmp_path, self.cache_file)
            restrict_file(self.cache_file)
        except OSError:
            pass
