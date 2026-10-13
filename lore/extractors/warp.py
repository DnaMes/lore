import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id, safe_copy_db
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class WarpExtractor(BaseExtractor):
    """Extract chat history from Warp Terminal."""

    def __init__(self):
        self.db_path = Path.home() / ".local" / "state" / "warp-terminal" / "warp.sqlite"
        self.db_paths = self._discover_db_paths()

    def _discover_db_paths(self):
        paths = [self.db_path]
        for candidate in discover_home_marker_paths(".local/state/warp-terminal/warp.sqlite"):
            if candidate not in paths:
                paths.append(candidate)
        return [path for path in paths if path.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.WARP

    def is_available(self) -> bool:
        return len(self.db_paths) > 0

    def source_description(self) -> str:
        return "~/.local/state/warp-terminal/warp.sqlite (ai_queries and agent_tasks tables)"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        original_db_path = self.db_path
        for db_path in self.db_paths:
            self.db_path = db_path
            work_db_path = safe_copy_db(db_path)

            try:
                conn = sqlite3.connect(f"file:{work_db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
            except sqlite3.Error as e:
                logger.warning("Could not open Warp database: %s", e)
                continue

            try:
                # Extract from ai_queries table
                cursor = conn.execute(
                    """
                    SELECT exchange_id, conversation_id, input, working_directory,
                           start_ts, model_id
                    FROM ai_queries
                    ORDER BY start_ts
                """
                )

                # Group queries by conversation_id
                conversations: Dict[str, List[Dict]] = {}
                for row in cursor:
                    conv_id = row["conversation_id"] or row["exchange_id"]
                    if conv_id not in conversations:
                        conversations[conv_id] = []

                    input_data = {}
                    raw_input = row["input"]
                    if raw_input:
                        try:
                            parsed = json.loads(raw_input)
                            if isinstance(parsed, list) and len(parsed) > 0:
                                input_data = parsed[0]
                            elif isinstance(parsed, dict):
                                input_data = parsed
                        except json.JSONDecodeError:
                            self._count("records_unknown")

                    conversations[conv_id].append(
                        {
                            "input": input_data,
                            "working_directory": row["working_directory"],
                            "start_ts": row["start_ts"],
                            "model_id": row["model_id"],
                        }
                    )

                assistant_by_conversation = self._load_assistant_messages(conn)

                for conv_id, queries in conversations.items():
                    # Database tool: files_matched counts the conversations considered.
                    self._count("files_matched")
                    try:
                        session = self._build_session(
                            conv_id,
                            queries,
                            assistant_by_conversation.get(conv_id, []),
                        )
                        self._count("files_parsed")
                        if session.message_count > 0 and self.should_import_session(session):
                            yield session
                    except Exception as e:
                        logger.warning("Failed to build Warp session %s: %s", conv_id, e)

            finally:
                conn.close()
                try:
                    if work_db_path != db_path:
                        work_db_path.unlink(missing_ok=True)
                        Path(str(work_db_path) + "-wal").unlink(missing_ok=True)
                        Path(str(work_db_path) + "-shm").unlink(missing_ok=True)
                except OSError as e:
                    logger.debug("Failed to cleanup temp db files: %s", e)
        self.db_path = original_db_path

    def _load_assistant_messages(self, conn) -> Dict[str, List[UnifiedMessage]]:
        messages_by_conversation: Dict[str, List[UnifiedMessage]] = {}
        try:
            cursor = conn.execute(
                """
                SELECT conversation_id, task, last_modified_at
                FROM agent_tasks
                ORDER BY last_modified_at, id
                """
            )
        except sqlite3.Error:
            return messages_by_conversation

        for row in cursor:
            conversation_id = row["conversation_id"]
            texts = self._extract_assistant_text_candidates_from_blob(row["task"])
            if not texts:
                continue

            timestamp = parse_timestamp(row["last_modified_at"])
            bucket = messages_by_conversation.setdefault(conversation_id, [])
            for idx, text in enumerate(texts):
                bucket.append(
                    UnifiedMessage(
                        role=Role.ASSISTANT,
                        content=text,
                        timestamp=timestamp + timedelta(milliseconds=idx),
                    )
                )

        return messages_by_conversation

    def _extract_assistant_text_from_blob(self, blob) -> str:
        candidates = self._extract_assistant_text_candidates_from_blob(blob)
        if not candidates:
            return ""
        return candidates[0][:2000]

    def _extract_assistant_text_candidates_from_blob(self, blob) -> List[str]:
        if not blob:
            return []

        if isinstance(blob, (bytes, bytearray)):
            raw_text = bytes(blob).decode("utf-8", errors="ignore")
        else:
            raw_text = str(blob)

        split_chunks = re.split(r"[^\x09\x0A\x0D\x20-\x7E\xA0-\uFFFF]+", raw_text)
        candidates: List[tuple[int, str]] = []

        for chunk_idx, chunk in enumerate(split_chunks):
            normalized = re.sub(r"\s+", " ", chunk).strip()
            if len(normalized) < 24:
                continue

            normalized = re.sub(r"\$[0-9a-fA-F-]{12,}", " ", normalized)
            normalized = re.sub(r"\b[a-zA-Z0-9+/]{48,}={0,2}\b", " ", normalized)
            normalized = re.sub(
                r"([.!?])\s*:?[\s-]*[A-Za-z0-9+/=]{8,}(?:\s+[A-Za-z0-9+/=]{1,8})*$",
                r"\1",
                normalized,
            )
            normalized = re.sub(r"^[='`:_\-\s]+", "", normalized)
            normalized = re.sub(r"^[a-z]\s+[a-z](?=[A-ZÄÖÜ])", "", normalized)
            normalized = re.sub(r"^[a-z](?=[A-ZÄÖÜ])", "", normalized)
            normalized = re.sub(r"\s+", " ", normalized).strip(" \t\n\r*")
            if len(normalized) < 24:
                continue

            if self._is_low_signal_warp_chunk(normalized):
                continue
            candidates.append((chunk_idx, normalized))

        if not candidates:
            return []

        scored: List[tuple[int, int, str]] = []
        for chunk_idx, text in candidates:
            score = self._score_warp_assistant_candidate(text)
            if score < 12:
                continue
            scored.append((chunk_idx, score, text))

        if not scored:
            return []

        scored.sort(key=lambda item: item[1], reverse=True)
        selected_ranked: List[tuple[int, str]] = []
        for chunk_idx, _, text in scored:
            lowered = text.lower()
            if any(
                lowered in existing.lower() or existing.lower() in lowered
                for _, existing in selected_ranked
            ):
                continue
            selected_ranked.append((chunk_idx, text))
            if len(selected_ranked) >= 24:
                break

        selected_ranked.sort(key=lambda item: item[0])
        return [text[:2000] for _, text in selected_ranked]

    def _is_low_signal_warp_chunk(self, text: str) -> bool:
        lowered = text.lower()
        noise_markers = [
            "this file provides guidance",
            "commands that will be commonly used",
            "repository guidelines",
            "project structure",
            "use for:",
            "do not use for:",
            "bundledskillid",
            "referenced_attachments",
            "scope':",
            "provider':",
            "the user is asking",
            "the user confirmed",
            "the orchestrator executed this command",
            "debug and troubleshoot",
            ".agents/skills",
            "skill.md",
            "agents.md",
        ]
        if any(marker in lowered for marker in noise_markers):
            return True

        if lowered.startswith("toolu_"):
            return True
        if re.fullmatch(r"\$?[0-9a-f-]{16,}", lowered):
            return True
        if text.count("/") >= 12 and len(text) < 320:
            return True

        alpha_count = sum(1 for ch in text if ch.isalpha())
        if alpha_count < max(12, len(text) // 5):
            return True
        return False

    def _score_warp_assistant_candidate(self, text: str) -> int:
        lowered = text.lower()
        score = 0

        score += min(sum(1 for ch in text if ch.isalpha()) // 20, 30)

        if "```" in text:
            score += 22
        if any(token in lowered for token in ["sudo", "shutdown", "systemctl", "bash"]):
            score += 24
        if any(
            token in lowered
            for token in [
                "ich kann",
                "soll ich",
                "zum abbrechen",
                "das folgende kommando",
                "you can",
                "i can",
                "let me",
            ]
        ):
            score += 28
        if text.endswith((".", "!", "?", ":")):
            score += 8

        penalties = [
            "use for:",
            "do not use for:",
            "the user",
            "orchestrator",
            "bundledskillid",
            "referenced_attachments",
        ]
        if any(token in lowered for token in penalties):
            score -= 50
        if text.count("/") >= 10:
            score -= 18
        if re.search(r"\b[a-zA-Z0-9+/]{60,}={0,2}\b", text):
            score -= 40

        return score

    def _is_echo_or_noise_assistant(self, text: str, user_messages: List[str]) -> bool:
        normalized = re.sub(r"\s+", " ", text or "").strip().lower()
        if not normalized:
            return True

        for user_msg in user_messages:
            user_text = re.sub(r"\s+", " ", user_msg or "").strip().lower()
            if len(user_text) < 8:
                continue
            if normalized == user_text:
                return True

        if normalized.startswith("toolu_"):
            return True
        if normalized.count("/") >= 12 and len(normalized) < 320:
            return True
        return False

    def _assistant_relevance_score(self, text: str, user_messages: List[str]) -> int:
        lowered = (text or "").lower()
        if not lowered:
            return 0

        stop_words = {
            "kannst",
            "könntest",
            "bitte",
            "einfach",
            "machen",
            "system",
            "the",
            "that",
            "this",
            "with",
            "from",
        }
        user_tokens: set[str] = set()
        for msg in user_messages:
            for token in re.findall(r"[\wäöüÄÖÜß-]+", (msg or "").lower()):
                token = token.strip("-_")
                if len(token) < 4 or token in stop_words:
                    continue
                user_tokens.add(token)

        if not user_tokens:
            return 0

        score = 0
        for token in user_tokens:
            stem = token[:8]
            if token in lowered or (len(stem) >= 5 and stem in lowered):
                score += 8

        if any(tok in lowered for tok in ["sudo", "shutdown", "systemctl", "bash"]):
            score += 4
        return score

    def _retime_assistant_messages(
        self,
        user_messages: List[UnifiedMessage],
        assistant_messages: List[UnifiedMessage],
    ) -> None:
        if not user_messages or not assistant_messages:
            return

        user_count = len(user_messages)
        assistant_count = len(assistant_messages)

        if assistant_count == 1:
            target_user_idx = 0 if user_count > 1 else user_count - 1
            anchor = user_messages[target_user_idx].timestamp
            assistant_messages[0].timestamp = anchor + timedelta(milliseconds=500)
            return

        if user_count == 1:
            base = user_messages[0].timestamp
            for idx, assistant in enumerate(assistant_messages):
                assistant.timestamp = base + timedelta(milliseconds=500 + idx)
            return

        for idx, assistant in enumerate(assistant_messages):
            target_user_idx = round(idx * (user_count - 1) / (assistant_count - 1))
            target_user_idx = max(0, min(target_user_idx, user_count - 1))
            anchor = user_messages[target_user_idx].timestamp
            assistant.timestamp = anchor + timedelta(milliseconds=500 + idx)

    def _build_session(
        self,
        conv_id: str,
        queries: List[Dict],
        assistant_messages: List[UnifiedMessage],
    ) -> UnifiedSession:
        """Build a session from grouped queries."""
        messages = []
        user_texts: List[str] = []
        project_path = None
        created_at = None
        last_updated = None

        for query in queries:
            timestamp = parse_timestamp(query["start_ts"])

            if created_at is None or timestamp < created_at:
                created_at = timestamp
            if last_updated is None or timestamp > last_updated:
                last_updated = timestamp

            if project_path is None:
                project_path = query.get("working_directory")

            input_data = query.get("input", {})

            # Extract user query text from Warp's structure
            # Structure: {"Query": {"text": "...", "context": [...]}}
            text = ""
            if isinstance(input_data, dict):
                query_obj = input_data.get("Query", {})
                if isinstance(query_obj, dict):
                    text = query_obj.get("text", "")
                else:
                    text = str(query_obj)

            if text:
                user_texts.append(text)
                user_message = UnifiedMessage(
                    role=Role.USER,
                    content=text,
                    timestamp=timestamp,
                    model=query.get("model_id"),
                )
                messages.append(user_message)

        cleaned_assistant_messages = []
        for assistant in assistant_messages:
            if self._is_echo_or_noise_assistant(assistant.content or "", user_texts):
                continue
            cleaned_assistant_messages.append(assistant)

        if len(cleaned_assistant_messages) > 1:
            scored_messages: List[tuple[UnifiedMessage, int, int]] = []
            for message in cleaned_assistant_messages:
                content = message.content or ""
                relevance = self._assistant_relevance_score(content, user_texts)
                quality = self._score_warp_assistant_candidate(content)
                scored_messages.append((message, relevance, quality))

            relevant_messages = [
                (msg, rel, qual) for msg, rel, qual in scored_messages if rel > 0 and qual >= 12
            ]

            if relevant_messages:
                target_count = max(1, len(user_texts))
                if len(relevant_messages) <= target_count:
                    cleaned_assistant_messages = [msg for msg, _, _ in relevant_messages]
                elif target_count == 1:
                    best = max(relevant_messages, key=lambda item: (item[1], item[2]))
                    cleaned_assistant_messages = [best[0]]
                else:
                    selected_indices = set()
                    span = len(relevant_messages) - 1
                    for i in range(target_count):
                        idx = round(i * span / (target_count - 1))
                        selected_indices.add(idx)
                    cleaned_assistant_messages = [
                        relevant_messages[idx][0] for idx in sorted(selected_indices)
                    ][:target_count]
            else:
                fallback_target = max(1, min(len(user_texts), len(scored_messages)))
                ranked_fallback = sorted(
                    scored_messages,
                    key=lambda item: (item[2], item[1]),
                    reverse=True,
                )
                cleaned_assistant_messages = [
                    msg for msg, _, _ in ranked_fallback[:fallback_target]
                ]

        user_messages = [msg for msg in messages if msg.role == Role.USER]
        self._retime_assistant_messages(user_messages, cleaned_assistant_messages)

        messages.extend(cleaned_assistant_messages)
        messages.sort(key=lambda message: message.timestamp)

        if cleaned_assistant_messages:
            latest_assistant = max(msg.timestamp for msg in cleaned_assistant_messages)
            if last_updated is None or latest_assistant > last_updated:
                last_updated = latest_assistant

        if created_at is None:
            created_at = datetime.now()
        if last_updated is None:
            last_updated = datetime.now()

        return UnifiedSession(
            tool=Tool.WARP,
            session_id=conv_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(self.db_path),
        )
