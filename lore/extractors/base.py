import functools
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..core.models import AUTOMATED_ORIGINS, Role, TitleSource, Tool, UnifiedSession

logger = logging.getLogger(__name__)

# Coverage counters every extractor may report; an extractor may add its own
# keys (e.g. Antigravity's transcript/artifact split, #158) and they survive into
# `lore check --deep`.
DISCOVERY_KEYS = ("files_matched", "files_parsed", "records_unknown")

REEXTRACT_BUDGET_ENV = "LORE_PARSER_REEXTRACT_BUDGET"
DEFAULT_REEXTRACT_BUDGET = 150


def reextract_budget_from_env() -> Optional[int]:
    """Version-stale sources one extractor may re-parse per run (``None`` = unlimited).

    Unset and invalid values give the default; ``0`` re-parses no stale source this run
    (changed sources are still parsed); a negative value lifts the cap.
    """
    raw = os.environ.get(REEXTRACT_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_REEXTRACT_BUDGET
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", REEXTRACT_BUDGET_ENV, raw)
        return DEFAULT_REEXTRACT_BUDGET
    return None if value < 0 else value


def stamp_parser_version(extractor: object, session: UnifiedSession) -> None:
    """Give a session that still has no parser version the extractor's own."""
    if not session.parser_version:  # an explicit value is kept
        session.parser_version = getattr(extractor, "PARSER_VERSION", 0)


class BaseExtractor(ABC):
    """Base class for all tool-specific extractors."""

    # Minimum thresholds for importing sessions
    MIN_MESSAGES = 1
    MIN_CONTENT_SIZE = 0
    MIN_USER_PROMPTS = 3

    # Sessions with an automated origin (exec/cron/subagent) are stored below the
    # prompt floor and hidden from default views instead. An extractor that already
    # tagged such sessions before the policy existed sets this to False, so its
    # import set (and the index size) stays what it was until its own task opts in.
    AUTOMATED_BYPASSES_PROMPT_FLOOR = True

    # Bump when the extractor starts producing fields it did not before. An
    # indexed session stored with a lower version is parsed again even when its
    # source file is unchanged (``_source_unchanged``).
    PARSER_VERSION: int = 1

    def __init_subclass__(cls, **kwargs) -> None:
        """Stamp ``PARSER_VERSION`` on every yielded session in one place."""
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("extract_sessions")
        if original is not None and not getattr(original, "_stamps_parser_version", False):

            @functools.wraps(original)
            def extract_sessions(self) -> Iterator[UnifiedSession]:
                # Reset eagerly (generators run lazily) so a second run starts at zero.
                self._reset_discovery()
                # Call eagerly so errors raised at call time still surface there.
                sessions = original(self)
                return self._stamp_parser_version(sessions)

            extract_sessions._stamps_parser_version = True  # type: ignore[attr-defined]
            cls.extract_sessions = extract_sessions  # type: ignore[method-assign]

        finder = cls.__dict__.get("find_session_by_id")
        if finder is not None and not getattr(finder, "_stamps_parser_version", False):

            @functools.wraps(finder)
            def find_session_by_id(self, session_id: str) -> Optional[UnifiedSession]:
                session = finder(self, session_id)
                if session is not None:
                    stamp_parser_version(self, session)
                return session

            find_session_by_id._stamps_parser_version = True  # type: ignore[attr-defined]
            cls.find_session_by_id = find_session_by_id  # type: ignore[method-assign]

    def _stamp_parser_version(self, sessions: Iterable[UnifiedSession]) -> Iterator[UnifiedSession]:
        for session in sessions:
            stamp_parser_version(self, session)
            self._sessions_yielded = getattr(self, "_sessions_yielded", 0) + 1
            yield session

    def _consume_reextract_budget(self, versions: dict[str, int]) -> bool:
        """Spend one unit of this run's version-stale re-parse budget.

        The budget lives on the instance, so a new instance (a new run) starts
        full. Once it is spent a stale source is reused with its old version, and
        later runs keep draining the backlog.
        """
        state = self.__dict__.get("_reextract_state")
        if state is None:
            state = {"left": reextract_budget_from_env(), "used": 0, "logged": False}
            self._reextract_state = state
        if state["left"] is None:
            return True
        if state["left"] > 0:
            state["left"] -= 1
            state["used"] += 1
            return True
        if not state["logged"]:
            state["logged"] = True
            stale = sum(1 for v in versions.values() if v < self.PARSER_VERSION)
            logger.info(
                "%s: parser re-extract budget of %d spent; about %d version-stale sources "
                "(counted across all tools) wait for later runs",
                getattr(self, "tool", type(self).__name__),
                state["used"],
                max(0, stale - state["used"]),
            )
        return False

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

    @property
    def discovery(self) -> dict[str, int]:
        """Per-run source telemetry, reset at the start of every ``extract_sessions()``.

        ``files_matched``: candidate sources found (files, or database rows for
        database-backed tools); ``files_parsed``: sources that were read without
        error; ``records_unknown``: records the parser did not recognise. A tool
        that is available yet reports ``files_matched == 0`` has a stale glob.
        """
        counts = getattr(self, "_discovery", None)
        if counts is None:
            counts = dict.fromkeys(DISCOVERY_KEYS, 0)
            self._discovery = counts
        return counts

    @property
    def sessions_yielded(self) -> int:
        """Sessions the last ``extract_sessions()`` run produced."""
        return getattr(self, "_sessions_yielded", 0)

    def _count(self, key: str, n: int = 1) -> None:
        self.discovery[key] = self.discovery.get(key, 0) + n

    def _reset_discovery(self) -> None:
        self._discovery = dict.fromkeys(DISCOVERY_KEYS, 0)
        self._sessions_yielded = 0

    def source_description(self) -> str:
        """Human-readable globs/paths this extractor reads (``~`` for the home directory)."""
        return type(self).__name__

    @abstractmethod
    def extract_sessions(self) -> Iterator[UnifiedSession]:
        pass

    def _source_unchanged(self, path: Path) -> bool:
        """True when ``path`` is recorded as up to date, so it need not be parsed.

        The caller sets ``known_source_mtimes`` (source path -> mtime_ns of the
        last indexed copy). A match is remembered in ``skipped_sources`` so the
        caller can reuse the prior index entry instead of a fresh parse.
        Extractors opt in by calling this before parsing a source file.
        """
        known = getattr(self, "known_source_mtimes", None)
        if not known:
            return False
        recorded = known.get(str(path))
        if recorded is None:
            return False
        try:
            current = path.stat().st_mtime_ns
        except OSError:
            return False
        if current != recorded:
            return False
        # Invariant: ``known_source_parser_versions[path]`` is the minimum
        # ``parser_version`` among every session stored for that source, so one
        # stale session of a multi-session source re-parses the whole source. A
        # path missing from a supplied map counts as version 0 (indexed before
        # migration 17). Callers that supply no map get mtime-only reuse. Such a
        # source is re-parsed only while the per-run budget lasts; past it the
        # source is reused and keeps its old stored version.
        versions = getattr(self, "known_source_parser_versions", None)
        if (
            versions is not None
            and versions.get(str(path), 0) < self.PARSER_VERSION
            and self._consume_reextract_budget(versions)
        ):
            return False
        skipped = getattr(self, "skipped_sources", None)
        if skipped is None:
            skipped = set()
            self.skipped_sources = skipped
        skipped.add(str(path))
        return True

    def find_session_by_id(self, session_id: str) -> Optional[UnifiedSession]:
        """Locate a single session by id without a full archive scan (#127).

        Extractors with deterministic source-file layouts override this to
        parse only the matching source file (e.g. claude stores main
        sessions as ``<projectdir>/<session_id>.jsonl``). The default
        returns ``None`` so callers fall back to the cached bulk
        collection without paying any per-tool extraction cost.
        """
        return None

    def has_source_data(self) -> bool:
        """Whether a data directory exists, readable or not (#158).

        ``is_available`` answers "can I import anything"; this answers "is there
        something to inspect", which is what ``lore check --deep`` needs to report
        undecodable artifacts instead of a bare "not found".
        """
        return self.is_available()

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

        and it has >= MIN_USER_PROMPTS user prompts, unless its origin is automated.

        This filters out test sessions, typos, and abandoned conversations
        while preserving sessions with substantial content.
        """
        self._normalize_session(session)

        min_messages, min_content_size, min_user_prompts = self._effective_thresholds()

        message_count = len(session.messages)
        user_prompt_count = session.user_prompt_count

        bypassed = False
        if user_prompt_count < min_user_prompts:
            # Automated work is not dropped for few prompts: it is kept and hidden
            # from default views. The message/content floors below still apply.
            if session.origin in AUTOMATED_ORIGINS and self.AUTOMATED_BYPASSES_PROMPT_FLOOR:
                bypassed = True
            else:
                self._record_skip("too_few_user_prompts")
                return False

        # Always import if enough messages; short sessions need enough content.
        if message_count >= min_messages or (
            sum(len(msg.content or "") for msg in session.messages) >= min_content_size
        ):
            if bypassed:
                # Counted only for a session that is actually imported.
                self._count("automated_kept")
            return True
        self._record_skip("too_little_content")
        return False
