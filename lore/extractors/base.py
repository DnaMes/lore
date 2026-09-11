import logging
import os
from abc import ABC, abstractmethod
from typing import Iterator, Optional

from ..core.models import Role, TitleSource, Tool, UnifiedSession

logger = logging.getLogger(__name__)


class BaseExtractor(ABC):
    """Base class for all tool-specific extractors."""

    # Minimum thresholds for importing sessions
    MIN_MESSAGES = 1
    MIN_CONTENT_SIZE = 0
    MIN_USER_PROMPTS = 3

    @property
    @abstractmethod
    def tool(self) -> Tool:
        pass

    @property
    def skip_counts(self) -> dict[str, int]:
        """Per-extractor tally of sessions the quality filter dropped.

        Keyed by reason (``too_few_user_prompts`` / ``too_little_content``).
        Lets the sync job surface "imported X, skipped Y" instead of silently
        dropping sessions inside the generator (#1e). Lazily created so
        subclasses need no __init__ cooperation.
        """
        counts = getattr(self, "_skip_counts", None)
        if counts is None:
            counts = {}
            self._skip_counts = counts
        return counts

    def _record_skip(self, reason: str) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1

    @abstractmethod
    def extract_sessions(self) -> Iterator[UnifiedSession]:
        pass

    def find_session_by_id(self, session_id: str) -> Optional[UnifiedSession]:
        """Locate a single session by id without a full archive scan (#127).

        Extractors with deterministic source-file layouts override this to
        parse only the matching source file (e.g. claude stores main
        sessions as ``<projectdir>/<session_id>.jsonl``). The default
        returns ``None`` so callers fall back to the cached bulk
        collection without paying any per-tool extraction cost.
        """
        return None

    def is_available(self) -> bool:
        """Check if the tool's data is available on this system."""
        return True

    def _normalize_session(self, session: UnifiedSession) -> None:
        if not session.thread_id:
            try:
                from ..utils.paths import make_thread_id

                session.thread_id = make_thread_id(project_path=session.project_path)
            except Exception as exc:
                logger.debug(
                    "Failed to generate thread id for session %s: %s",
                    session.session_id,
                    exc,
                )

        if not session.thread_id:
            session.thread_id = f"session:{session.session_id[:12]}"

        if session.title and session.title.strip():
            if session.title_source is None:
                session.title_source = TitleSource.NATIVE
            return

        first_user_message = ""
        for message in session.messages:
            if message.role == Role.USER and (message.content or "").strip():
                first_user_message = message.content.strip()
                break

        if first_user_message:
            words = first_user_message.split()[:10]
            title = " ".join(words)
            if len(title) > 80:
                title = title[:77] + "..."
            session.title = title
            session.title_source = TitleSource.FIRST_MESSAGE
            return

        session.title = f"{session.tool.value.replace('-', ' ').title()} Session {session.created_at.strftime('%Y-%m-%d')}"
        session.title_source = TitleSource.FALLBACK

    def _effective_thresholds(self) -> tuple[int, int, int]:
        profile = os.environ.get("LORE_IMPORT_PROFILE", "relaxed").strip().lower()
        if profile == "strict":
            min_messages, min_content, min_prompts = (3, 2048, 5)
        else:
            min_messages, min_content, min_prompts = (
                self.MIN_MESSAGES,
                self.MIN_CONTENT_SIZE,
                self.MIN_USER_PROMPTS,
            )
        # Explicit override wins over the profile so users can keep more
        # short sessions (e.g. LORE_MIN_USER_PROMPTS=2) without flipping
        # the whole profile. Invalid values fall back to the profile default.
        override = os.environ.get("LORE_MIN_USER_PROMPTS", "").strip()
        if override:
            try:
                min_prompts = max(0, int(override))
            except ValueError:
                logger.warning("Ignoring invalid LORE_MIN_USER_PROMPTS=%r", override)
        return (min_messages, min_content, min_prompts)

    def should_import_session(self, session: UnifiedSession) -> bool:
        """
        Determine if a session should be imported based on quality thresholds.

        A session is imported if:
        - It has >= MIN_MESSAGES messages, OR
        - Total content size >= MIN_CONTENT_SIZE bytes

        This filters out test sessions, typos, and abandoned conversations
        while preserving sessions with substantial content.
        """
        self._normalize_session(session)

        min_messages, min_content_size, min_user_prompts = self._effective_thresholds()

        message_count = len(session.messages)
        user_prompt_count = session.user_prompt_count

        if user_prompt_count < min_user_prompts:
            self._record_skip("too_few_user_prompts")
            return False

        # Always import if enough messages
        if message_count >= min_messages:
            return True

        # For short sessions, check content size
        total_content_size = sum(len(msg.content or "") for msg in session.messages)

        if total_content_size >= min_content_size:
            return True
        self._record_skip("too_little_content")
        return False
