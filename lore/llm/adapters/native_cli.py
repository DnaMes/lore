"""Native-CLI provider boundary (#180): detect, invoke, parse, bound, cancel.

Every adapter in this package runs the *installed, unmodified* CLI with the
user's own login and parses the output format that CLI documents. This module
owns the shared part of that contract so no adapter can invent its own:

* **Detection.** ``shutil.which`` plus ``<executable> --version``, then a
  capability probe against ``<executable> <subcommand> --help``. A missing or
  older executable reports ``available=False`` with a reason; it never becomes a
  call and never falls back to an API provider.
* **Invocation.** The prompt travels on standard input by default, so transcript
  text never appears in the argument vector. The child runs in a temporary
  working directory in its own process group.
* **Credentials.** Lore never reads a credential file and never passes a token.
  The child inherits the user's environment *minus* the credential variables
  listed in :data:`CREDENTIAL_ENV_VARS`, so the answer can only come from the
  CLI's own native login — not from a key Lore holds for another provider.
* **Bounds.** The request timeout and the execution policy's prompt bound are
  enforced, and the cancellation token kills the process group.

Failure states are distinct and typed (``errors``): unavailable, auth, timeout,
cancelled, execution error, malformed output. None of them retries on another
provider.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

from ...utils.redact import redact
from ..base import LLMConfig, LLMProvider, LLMResponse
from ..gate import ensure_cloud_allowed
from .contract import (
    AdapterResult,
    AuthMode,
    CancellationToken,
    CliProbe,
    ExecutionPolicy,
    OutputFormat,
    PromptTransport,
    ProviderRequest,
)
from .errors import (
    AdapterAuthError,
    AdapterCancelled,
    AdapterExecutionError,
    AdapterOutputError,
    AdapterPromptTooLarge,
    AdapterTimeout,
    AdapterUnavailable,
)

logger = logging.getLogger(__name__)

# Credential-bearing variables are removed from the child environment: a
# native-CLI run authenticates through the CLI's own login, never with a key
# Lore (or another provider) happens to hold (#180, docs/PRIVACY.md).
CREDENTIAL_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    }
)

# Substrings a CLI prints when it has no usable login. Used only to report the
# failure as an auth failure instead of a generic non-zero exit.
AUTH_FAILURE_MARKERS = (
    "not logged in",
    "not authenticated",
    "authentication",
    "unauthorized",
    "invalid api key",
    "login required",
    "missing api key",
    "no credentials",
    "please log in",
)

VERSION_TIMEOUT_SECONDS = 10
# How often a running call checks the deadline and the cancellation token.
POLL_SECONDS = 0.05
_STDERR_LIMIT = 400
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _one_line(text: str, limit: int = _STDERR_LIMIT) -> str:
    """Last ``limit`` characters of a redacted, single-line diagnostic."""
    cleaned = " ".join(redact(text or "").text.split())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


class NativeCliAdapter(LLMProvider):
    """Shared behaviour for every installed-CLI provider.

    Subclasses set the class attributes, then implement :meth:`build_argv`
    (documented invocation) and :meth:`parse_output` (documented structured
    output). Everything else — detection, bounds, cancellation, environment and
    credential boundaries — lives here so it cannot drift per adapter.
    """

    PROVIDER = ""
    EXECUTABLE = ""
    AUTH_MODE = AuthMode.NATIVE_CLI
    # "" means: pass no model flag and let the CLI use its configured default.
    DEFAULT_MODEL = ""
    OUTPUT_FORMAT = OutputFormat.JSON
    PROMPT_TRANSPORT = PromptTransport.STDIN
    MAX_PROMPT_CHARS = 400_000
    # Help subcommand and the flags that must be documented for the pinned
    # interface to be the one installed.
    HELP_ARGV: Tuple[str, ...] = ()
    REQUIRED_HELP_TOKENS: Tuple[str, ...] = ()

    def __init__(
        self,
        config: LLMConfig,
        *,
        allow_cloud: bool = False,
        request: Optional[ProviderRequest] = None,
    ):
        super().__init__(config)
        # A directly constructed adapter must refuse a cloud send without opt-in
        # exactly like a factory-built one (docs/PRIVACY.md).
        self.allow_cloud = allow_cloud
        self.request = request or self.build_request()
        self._probe: Optional[CliProbe] = None

    # -- the request contract -------------------------------------------------

    @classmethod
    def execution_policy(cls) -> ExecutionPolicy:
        """The policy this adapter runs under; model-dependent argv is added per call."""
        return ExecutionPolicy(
            executable=cls.EXECUTABLE,
            prompt_transport=cls.PROMPT_TRANSPORT,
            output_format=cls.OUTPUT_FORMAT,
            max_prompt_chars=cls.MAX_PROMPT_CHARS,
        )

    def build_request(self, cancellation: Optional[CancellationToken] = None) -> ProviderRequest:
        return ProviderRequest(
            provider=self.PROVIDER,
            model=self.config.model or self.DEFAULT_MODEL,
            auth_mode=self.AUTH_MODE,
            timeout=float(self.config.timeout),
            policy=self.execution_policy(),
            cancellation=cancellation or CancellationToken(),
            task=self.config.task,
            prompt_version=self.config.prompt_version,
            cache_scope=self.config.cache_scope,
        )

    def provider_identity(self) -> Dict[str, Any]:
        """Who answers this request, and through which executable/login."""
        probe = self.probe()
        return {
            "provider": self.PROVIDER,
            "model": self.request.model or "cli-default",
            "auth_mode": self.AUTH_MODE.value,
            "executable": probe.executable,
            "path": probe.path or "",
            "version": probe.version,
        }

    def _cache_identity_extras(self) -> Dict[str, Any]:
        """The explicit request and the resolved CLI are part of a cached answer (#186)."""
        return {
            "request": {
                "auth_mode": self.AUTH_MODE.value,
                "timeout": self.request.timeout,
                "policy": self.request.policy.to_dict(),
                "cli_version": self.probe().version,
            }
        }

    # -- detection ------------------------------------------------------------

    def probe(self) -> CliProbe:
        """Availability, version and capabilities of the installed CLI (once per adapter)."""
        if self._probe is None:
            self._probe = self._detect()
        return self._probe

    def availability(self) -> CliProbe:
        """Alias of :meth:`probe` for callers that only ask about availability."""
        return self.probe()

    def _detect(self) -> CliProbe:
        path = shutil.which(self.EXECUTABLE)
        if path is None:
            return CliProbe(
                self.PROVIDER,
                self.EXECUTABLE,
                False,
                reason=f"no '{self.EXECUTABLE}' executable on PATH",
            )
        try:
            completed = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=VERSION_TIMEOUT_SECONDS,
                env=self.child_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CliProbe(
                self.PROVIDER,
                self.EXECUTABLE,
                False,
                path=path,
                reason=f"'{self.EXECUTABLE} --version' could not run ({type(exc).__name__})",
            )
        if completed.returncode != 0:
            return CliProbe(
                self.PROVIDER,
                self.EXECUTABLE,
                False,
                path=path,
                reason=f"'{self.EXECUTABLE} --version' exited {completed.returncode}",
            )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
        version = self.parse_version(output)
        capabilities, missing_reason = self._probe_capabilities(path)
        if missing_reason:
            return CliProbe(
                self.PROVIDER,
                self.EXECUTABLE,
                False,
                path=path,
                version=version,
                reason=missing_reason,
            )
        return CliProbe(
            self.PROVIDER,
            self.EXECUTABLE,
            True,
            path=path,
            version=version,
            capabilities=capabilities,
        )

    def parse_version(self, output: str) -> str:
        """First version-shaped token of ``<executable> --version``, else its first line."""
        first_line = output.splitlines()[0].strip() if output else ""
        match = _VERSION_RE.search(first_line) or _VERSION_RE.search(output)
        return match.group(0) if match else first_line[:120]

    def _probe_capabilities(self, path: str) -> Tuple[Tuple[str, ...], str]:
        """Check that the installed CLI documents the flags this adapter uses."""
        if not self.REQUIRED_HELP_TOKENS:
            return (), ""
        argv = [path, *self.HELP_ARGV, "--help"]
        label = " ".join([self.EXECUTABLE, *self.HELP_ARGV, "--help"])
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=VERSION_TIMEOUT_SECONDS,
                env=self.child_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return (), f"'{label}' could not run ({type(exc).__name__})"
        if completed.returncode != 0:
            return (), f"'{label}' exited {completed.returncode}"
        help_text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        missing = [token for token in self.REQUIRED_HELP_TOKENS if token not in help_text]
        if missing:
            return (), f"installed '{self.EXECUTABLE}' does not document {', '.join(missing)}"
        return tuple(self.REQUIRED_HELP_TOKENS), ""

    def is_available(self) -> bool:
        return self.probe().available

    # -- invocation -----------------------------------------------------------

    def child_env(self) -> Dict[str, str]:
        """The user's environment without the credential variables Lore must not pass."""
        return {
            name: value for name, value in os.environ.items() if name not in CREDENTIAL_ENV_VARS
        }

    def compose_prompt(self, prompt: str, system_prompt: Optional[str]) -> str:
        payload = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        limit = self.request.policy.max_prompt_chars
        if limit and len(payload) > limit:
            raise AdapterPromptTooLarge(self.PROVIDER, len(payload), limit)
        return payload

    def build_argv(self, probe: CliProbe) -> List[str]:
        """The documented, unmodified-CLI invocation for one call."""
        raise NotImplementedError

    def parse_output(self, raw: str) -> AdapterResult:
        """Parse the documented structured output into the common job contract."""
        raise NotImplementedError

    def run(self, prompt: str, system_prompt: Optional[str] = None) -> AdapterResult:
        """Run one bounded, cancellable CLI call and parse its answer."""
        probe = self.probe()
        if not probe.available:
            raise AdapterUnavailable(self.PROVIDER, probe.reason, version=probe.version)
        self.request.cancellation.raise_if_cancelled(self.PROVIDER)
        payload = self.compose_prompt(prompt, system_prompt)
        argv = self.build_argv(probe)
        started = time.monotonic()
        stdout, stderr = self._invoke(argv, payload)
        result = self.parse_output(stdout)
        return AdapterResult(
            content=result.content,
            model=self.request.model or "cli-default",
            structured=result.structured,
            tokens_used=result.tokens_used,
            duration_seconds=time.monotonic() - started,
            version=probe.version,
            partial=result.partial,
        )

    def _invoke(self, argv: List[str], payload: str) -> Tuple[str, str]:
        cwd_context = (
            tempfile.TemporaryDirectory()
            if self.request.policy.cwd == "tempdir"
            else nullcontext(None)
        )
        with cwd_context as cwd:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=self.child_env(),
                # Own process group, so a timeout or cancellation kills the
                # CLI and anything it started.
                start_new_session=True,
            )
            stdout, stderr = self._communicate(proc, payload)
        if proc.returncode != 0:
            raise self._failure(proc.returncode, stderr, stdout)
        return stdout, stderr

    def _communicate(self, proc: "subprocess.Popen[str]", payload: str) -> Tuple[str, str]:
        """Feed the prompt, then poll so the deadline and the token are honoured."""
        token = self.request.cancellation
        deadline = time.monotonic() + self.request.timeout
        first = True
        try:
            while True:
                if token.cancelled:
                    raise AdapterCancelled(self.PROVIDER)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterTimeout(self.PROVIDER, self.request.timeout)
                try:
                    return proc.communicate(
                        input=payload if first else None,
                        timeout=min(POLL_SECONDS, remaining),
                    )
                except subprocess.TimeoutExpired:
                    first = False
        finally:
            if proc.poll() is None:
                self._kill(proc)

    def _kill(self, proc: "subprocess.Popen[str]") -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (AttributeError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.communicate(timeout=self.request.policy.kill_grace_seconds)
        except (subprocess.TimeoutExpired, ValueError):
            pass

    def _failure(self, returncode: int, stderr: str, stdout: str) -> Exception:
        detail = _one_line(stderr) or _one_line(stdout) or "no stderr"
        if any(marker in detail.lower() for marker in AUTH_FAILURE_MARKERS):
            return AdapterAuthError(self.PROVIDER, f"exit {returncode}: {detail}")
        return AdapterExecutionError(self.PROVIDER, returncode, detail)

    # -- LLMProvider ----------------------------------------------------------

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        # Sampling limits belong to the CLI's own configuration; passing them
        # would mean inventing flags the CLI does not document.
        del max_tokens, temperature
        ensure_cloud_allowed(self.PROVIDER, self.allow_cloud)
        result = self.run(prompt, system_prompt)
        return LLMResponse(
            content=result.content,
            model=result.model,
            tokens_used=result.tokens_used,
            cached=False,
            metadata={
                "adapter": self.PROVIDER,
                "auth_mode": self.AUTH_MODE.value,
                "cli_version": result.version,
                "duration_seconds": round(result.duration_seconds, 3),
                "partial": result.partial,
                "request": self.request.to_dict(),
            },
        )

    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[LLMResponse]:
        return [self.generate(prompt, system_prompt) for prompt in prompts]

    def get_model_info(self) -> Dict[str, Any]:
        probe = self.probe()
        return {
            "provider": self.PROVIDER,
            "model": self.request.model or "cli-default",
            "auth_mode": self.AUTH_MODE.value,
            "executable": probe.path or probe.executable,
            "version": probe.version,
            "available": probe.available,
            "unavailable_reason": probe.reason,
            "capabilities": list(probe.capabilities),
            "timeout": self.request.timeout,
            "output_format": self.request.policy.output_format.value,
        }


def parse_json_document(raw: str, provider: str, label: str) -> Dict[str, Any]:
    """Parse one documented JSON document, or raise :class:`AdapterOutputError`."""
    import json

    text = (raw or "").strip()
    if not text:
        raise AdapterOutputError(provider, f"no output from {label}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdapterOutputError(
            provider, f"{label} output is not JSON ({exc.msg} at line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterOutputError(provider, f"{label} output is not a JSON object")
    return payload
