import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.paths import lore_home, restrict_file
from .base import LLMConfig, LLMProvider, LLMResponse
from .cache_io import sweep_temp_files
from .gate import ensure_cloud_allowed

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    def __init__(self, config: LLMConfig, allow_cloud: bool = False):
        super().__init__(config)
        # Transport-boundary opt-in: a directly constructed provider must refuse
        # to send without it just like one built by the factory (#PRIVACY.md).
        self.allow_cloud = allow_cloud
        self._client = None
        self._cache_dir_path = Path(config.cache_dir or lore_home() / "llm_cache")
        self._cache_dir: Optional[Path] = None

    def _ensure_cache_dir(self) -> Optional[Path]:
        if not self.config.cache_enabled:
            return None
        if self._cache_dir is None:
            cache_dir = self._cache_dir_path
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Temp files a hard kill left behind would otherwise live here forever.
            sweep_temp_files(cache_dir)
            self._cache_dir = cache_dir
        return self._cache_dir

    # Gemini CLI OAuth client credentials — read from env vars or ~/.gemini/oauth_creds.json.
    # Set GEMINI_CLIENT_ID / GEMINI_CLIENT_SECRET env vars to override.
    DEFAULT_GEMINI_CLIENT_ID = os.environ.get("GEMINI_CLIENT_ID", "")
    DEFAULT_GEMINI_CLIENT_SECRET = os.environ.get("GEMINI_CLIENT_SECRET", "")

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import google.generativeai as genai

            # Method 1: API Key (highest priority if provided)
            api_key = self.config.api_key
            if not api_key:
                api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

            if api_key:
                genai.configure(api_key=api_key)
                self._client = genai
                return self._client

            # Method 2: OAuth2 credentials from config
            client_id = self.config.oauth_client_id or os.environ.get("GOOGLE_CLIENT_ID")
            client_secret = self.config.oauth_client_secret or os.environ.get(
                "GOOGLE_CLIENT_SECRET"
            )
            refresh_token = self.config.oauth_refresh_token or os.environ.get(
                "GOOGLE_REFRESH_TOKEN"
            )

            if client_id and client_secret and refresh_token:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials

                credentials = Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    client_secret=client_secret,
                    token_uri="https://oauth2.googleapis.com/token",
                )

                # Refresh the token
                credentials.refresh(Request())

                genai.configure(credentials=credentials)
                self._client = genai
                return self._client

            # Method 3: Application Default Credentials (ADC)
            if self.config.use_adc or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                import google.auth

                credentials, project = google.auth.default()
                genai.configure(credentials=credentials)
                self._client = genai
                return self._client

            # No valid authentication found
            raise ValueError(
                "Gemini authentication not found. Use one of:\n"
                "  1. Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable\n"
                "  2. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN for OAuth\n"
                "  3. Run 'gcloud auth application-default login' and set use_adc=True\n"
                "  4. Pass credentials in config\n"
                "The Gemini CLI's own login is not reused here: select the "
                "'gemini-cli' adapter to run the CLI with its native login instead."
            )

        except ImportError as e:
            raise ImportError(
                f"Required package not installed: {e}. Install with: pip install google-generativeai google-auth"
            )

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        ensure_cloud_allowed("gemini", self.allow_cloud)
        cache_key = None
        if self.config.cache_enabled:
            cache_key = self._get_cache_key(prompt, system_prompt)
            cache_identity = self.cache_identity(prompt, system_prompt)
            cached = self._load_from_cache(cache_key)
            if cached:
                return cached

        client = self._get_client()

        model = client.GenerativeModel(
            self.config.model,
            system_instruction=system_prompt if system_prompt else None,
        )

        generation_config = client.types.GenerationConfig(
            max_output_tokens=max_tokens or self.config.max_tokens,
            temperature=(temperature if temperature is not None else self.config.temperature),
        )

        response = model.generate_content(prompt, generation_config=generation_config)

        content = response.text

        result = LLMResponse(
            content=content,
            model=self.config.model,
            tokens_used=(
                response.usage_metadata.total_token_count
                if hasattr(response, "usage_metadata")
                else 0
            ),
            cached=False,
        )

        if self.config.cache_enabled and cache_key:
            self._save_to_cache(cache_key, result, cache_identity)

        return result

    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt) for p in prompts]

    def is_available(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception as exc:
            # Any backend failure means "not available"; keep the broad net
            # but leave a diagnostic trail (#115).
            logger.debug("Gemini provider unavailable: %s", exc)
            return False

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "provider": "gemini",
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

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
                logger.debug("Ignoring unreadable Gemini cache entry: %s", exc)
        return None

    def _save_to_cache(self, cache_key: str, response: LLMResponse, identity=None):
        cache_root = self._ensure_cache_dir()
        if cache_root is None:
            return
        cache_file = cache_root / f"{cache_key}.json"
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=cache_root, prefix=f".{cache_key}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "content": response.content,
                        "model": response.model,
                        "tokens_used": response.tokens_used,
                        # The request this result belongs to (#186): an old
                        # entry stays auditable after the code or prompt moves on.
                        "request": identity or {},
                    },
                    f,
                )
            os.replace(tmp_path, cache_file)  # atomic: concurrent workers cannot see half a file
            tmp_path = None
            restrict_file(cache_file)
        except OSError as exc:
            logger.debug("Could not write cache entry: %s", exc)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
