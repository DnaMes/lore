"""Jev LLM provider: text-only delegate broker lanes (local, fastcheap)."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..utils.paths import lore_home
from .base import LLMConfig, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

ALLOWED_LANES = frozenset({"local", "fastcheap"})
DEFAULT_MODEL_LABEL = "local,fastcheap"
_DELEGATE_BIN = "delegate"


class JevError(Exception):
    """All configured delegate lanes failed."""


def resolve_jev_lanes() -> Tuple[str, ...]:
    """Lane order from ``LORE_JEV_LANES`` (comma-separated), default local then fastcheap."""
    raw = os.environ.get("LORE_JEV_LANES", DEFAULT_MODEL_LABEL)
    lanes = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not lanes:
        lanes = ("local", "fastcheap")
    return lanes


def validate_jev_lanes(lanes: Sequence[str]) -> None:
    """Reject agentic or auto lanes that must not process untrusted transcript text."""
    for lane in lanes:
        if lane not in ALLOWED_LANES:
            allowed = ", ".join(sorted(ALLOWED_LANES))
            raise ValueError(f"disallowed jev lane '{lane}'; allowed: {allowed}")


def _stdin_payload(prompt: str, system_prompt: Optional[str]) -> str:
    if system_prompt:
        return f"{system_prompt}\n\n{prompt}"
    return prompt


def _lane_failure_reason(returncode: int, stdout: str) -> str:
    if returncode == 3:
        return "quota exhausted"
    if returncode != 0:
        return f"exit {returncode}"
    if not stdout.strip():
        return "empty response"
    return "unknown failure"


class JevProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._lanes = resolve_jev_lanes()
        validate_jev_lanes(self._lanes)
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            self._cache_dir = self._cache_dir_path
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def is_available(self) -> bool:
        return shutil.which(_DELEGATE_BIN) is not None

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        del max_tokens, temperature  # delegate broker picks limits per lane
        cache_key = None
        if self.config.cache_enabled:
            cache_key = self._get_cache_key(prompt, system_prompt)
            cached = self._load_from_cache(cache_key)
            if cached:
                return cached

        content, lane_used = self._run_delegate(prompt, system_prompt)
        result = LLMResponse(
            content=content,
            model=f"jev/{lane_used}",
            tokens_used=0,
            cached=False,
            metadata={"lane": lane_used, "lanes": ",".join(self._lanes)},
        )

        if self.config.cache_enabled and cache_key:
            self._save_to_cache(cache_key, result)

        return result

    def _run_delegate(self, prompt: str, system_prompt: Optional[str]) -> Tuple[str, str]:
        stdin_text = _stdin_payload(prompt, system_prompt)
        failures: List[str] = []

        for lane in self._lanes:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    completed = subprocess.run(
                        [_DELEGATE_BIN, "--type", lane],
                        input=stdin_text,
                        text=True,
                        capture_output=True,
                        timeout=self.config.timeout,
                        cwd=tmp,
                    )
            except subprocess.TimeoutExpired:
                failures.append(f"{lane}: timeout")
                continue

            stdout = completed.stdout or ""
            if completed.returncode == 0 and stdout.strip():
                return stdout.strip(), lane

            reason = _lane_failure_reason(completed.returncode, stdout)
            failures.append(f"{lane}: {reason}")
            logger.debug(
                "jev lane %s failed (rc=%s): %s",
                lane,
                completed.returncode,
                reason,
            )

        raise JevError("all lanes failed: " + "; ".join(failures))

    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt) for p in prompts]

    def get_model_info(self) -> Dict[str, object]:
        return {
            "provider": "jev",
            "model": self.config.model or DEFAULT_MODEL_LABEL,
            "lanes": list(self._lanes),
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

    def _get_cache_key(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        combined = f"{system_prompt or ''}|{prompt}|{','.join(self._lanes)}"
        return hashlib.sha256(combined.encode()).hexdigest()

    def _load_from_cache(self, cache_key: str) -> Optional[LLMResponse]:
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return None
        cache_file = cache_root / f"{cache_key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                return LLMResponse(
                    content=data["content"],
                    model=data["model"],
                    tokens_used=data.get("tokens_used", 0),
                    cached=True,
                )
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                logger.debug("Ignoring unreadable Jev cache entry: %s", exc)
        return None

    def _save_to_cache(self, cache_key: str, response: LLMResponse) -> None:
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return
        cache_file = cache_root / f"{cache_key}.json"
        try:
            with open(cache_file, "w") as f:
                json.dump(
                    {
                        "content": response.content,
                        "model": response.model,
                        "tokens_used": response.tokens_used,
                    },
                    f,
                )
        except OSError as exc:
            logger.debug("Could not write Jev cache entry: %s", exc)
