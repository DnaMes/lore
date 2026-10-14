"""Redact secret-shaped strings from free text before export or LLM prompts.

Each pattern is compiled once at import and is linear-time: no nested quantifiers, and every
pattern that can fail after a long run of word characters is anchored with a lookbehind so the
run is not rescanned at each offset. Replacement markers are ``[REDACTED:<kind>]`` and never match
any pattern again, so ``redact`` is idempotent.

Kinds:

- ``private_key``: PEM ``-----BEGIN ... PRIVATE KEY-----`` through the matching
  ``-----END ...-----``; an unclosed block is redacted to the end of the input.
- ``aws_access_key``: ``AKIA|ASIA|AIDA|AROA`` plus 16 uppercase alphanumerics.
- ``aws_secret``: value after ``aws_secret_access_key`` with ``=``/``:`` (40 base64-ish chars).
- ``github_token``: ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_`` + 36+ alnum; ``github_pat_``.
- ``gitlab_token``: ``glpat-`` + 20+ chars. ``npm_token``: ``npm_`` + 36 alnum.
  ``pypi_token``: ``pypi-`` + 50+ chars. ``huggingface_token``: ``hf_`` + 34+ alnum.
- ``slack_token``: ``xox[baprs]-`` + 10+ chars.
- ``openai_key``: ``sk-`` + 20+ chars (covers ``sk-proj-``); ``anthropic_key``: ``sk-ant-``.
- ``google_api_key``: ``AIza`` + 35 chars. ``stripe_key``: ``sk_live_``/``rk_live_``/``pk_live_``.
- ``jwt``: three base64url segments, the first starting with ``eyJ``.
- ``bearer_token``: the credential of an ``Authorization:`` header (any scheme) or of a
  ``bearer <token>`` phrase; the scheme word is kept.
- ``cookie``: the value of a ``Cookie:`` / ``Set-Cookie:`` header line.
- ``url_password``: ``scheme://user:password@host`` (password only).
- ``secret_assignment``: assignment (``=``/``:``, shell, ``.env``, YAML, JSON, quoted or not) to a
  key whose name ends in password/passwd/pwd/secret/token/api_key/access_key/private_key/
  credential, and ``--password``-style CLI flags. Placeholders, numbers, booleans, environment
  references and dotted code references such as ``request.password`` are left alone.

Generic high-entropy hex/base64, UUIDs, git SHAs and hashes are intentionally not matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Optional

_MARKER = "[REDACTED:"
_PEM_BEGIN = re.compile(r"-----BEGIN [^-\n]{0,60}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [^-\n]{0,60}PRIVATE KEY-----")

_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")
_AWS_SECRET = re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)([A-Za-z0-9/+=]{40})")
_GITHUB_CLASSIC = re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")
_GITHUB_PAT = re.compile(r"github_pat_[A-Za-z0-9_]{50,}")
_GITLAB = re.compile(r"glpat-[A-Za-z0-9_-]{20,}")
_NPM = re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")
_PYPI = re.compile(r"\bpypi-[A-Za-z0-9_-]{50,}")
_HUGGINGFACE = re.compile(r"\bhf_[A-Za-z0-9]{34,}")
_SLACK = re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")
_ANTHROPIC = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
_OPENAI = re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}")
_GOOGLE = re.compile(r"AIza[A-Za-z0-9_-]{35}")
_STRIPE = re.compile(r"(?:sk_live_|rk_live_|pk_live_)[A-Za-z0-9]{16,}")
# The lookbehind keeps a long "eyJeyJ..." run from being rescanned at every offset (quadratic).
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
_AUTH_HEADER = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([\"']?authorization[\"']?\s*[:=][ \t]*[\"']?)([^\r\n\"']{6,})"
)
_AUTH_SCHEME = re.compile(r"(?i)^([A-Za-z][A-Za-z0-9_-]{0,20}[ \t]+)(.+)$")
_BEARER_WORD = re.compile(r"(\bbearer[ \t]+)([^\s\"',;]{20,})")
_COOKIE = re.compile(r"(?i)(?<![A-Za-z0-9_-])((?:set-)?cookie[ \t]*:[ \t]*)([^\r\n]{8,})")
_URL_PASSWORD = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]{0,31}://)([^:/@\s]+):([^@/\s]+)@")

_KEY_NAME = (
    r"(?:[A-Za-z_][A-Za-z0-9_.-]{0,80}?)?"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|account[_-]?key|shared[_-]?access[_-]?signature)"
)
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:export[ \t]+)?"
    r"(?P<key>[\"']?" + _KEY_NAME + r"[\"']?)"
    r"(?P<sep>\]?[ \t]*[=:][ \t]*)"
    r'(?:"(?P<dq>(?:[^"\\\r\n]|\\.){4,500})"'
    r"|'(?P<sq>(?:[^'\\\r\n]|\\.){4,500})'"
    r"|(?P<uv>[^\s\"',;]{8,}))"
)
_CLI_FLAG = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(--(?:password|passwd|token|api-key|apikey|secret|client-secret)"
    r"(?P<sep>=|[ \t]+))([^\s\"']{4,})"
)
_SAS_SIG = re.compile(r"(?i)([?&;]sig=)([A-Za-z0-9%/+=_-]{8,})")

_PLACEHOLDER_LOWER = frozenset(
    {"changeme", "example", "null", "none", "true", "false", "xxxx", "password", "secret"},
)
_CODE_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_CALL_OR_INDEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:\([^)]*\)|\[[^\]]*\])$")
_SECRET_WORDS = frozenset(
    {"password", "passwd", "pwd", "secret", "token", "key", "api_key", "apikey", "credentials"}
)
_NUMERIC = re.compile(r"^[0-9._-]+$")


@dataclass(frozen=True)
class RedactionResult:
    text: str
    count: int
    kinds: dict[str, int]


def _is_placeholder(value: str) -> bool:
    if value.startswith(_MARKER):
        return True
    low = value.lower().strip()
    if low in _PLACEHOLDER_LOWER or _NUMERIC.match(low):
        return True
    if value[:1] in ("<", "$", "%", "{") or value.startswith(("{{", "${")):
        return True
    if low.startswith("your_") or low.startswith("your-"):
        return True
    if "os.environ" in value or "process.env" in value or "getenv(" in low:
        return True
    if _CALL_OR_INDEX.match(value):  # call/index expression, not a literal secret
        return True
    if _CODE_REFERENCE.match(value) and value.rsplit(".", 1)[-1].lower() in _SECRET_WORDS:
        return True
    if set(value) <= {"x", "X", "*", "."}:
        return True
    return False


def _sub_count(
    text: str,
    pattern: re.Pattern[str],
    kind: str,
    kinds: dict[str, int],
    repl: Callable[[re.Match[str]], Optional[str]],
) -> str:
    def _repl(m: re.Match[str]) -> str:
        replacement = repl(m)
        if replacement is None:  # left alone (placeholder)
            return m.group(0)
        kinds[kind] = kinds.get(kind, 0) + 1
        return replacement

    return pattern.sub(_repl, text)


def _marker(kind: str) -> str:
    return f"{_MARKER}{kind}]"


def _simple_marker(kind: str) -> Callable[[re.Match[str]], Optional[str]]:
    marker = _marker(kind)
    return lambda _m: marker


def _redact_pem(text: str, kinds: dict[str, int]) -> str:
    """Single forward pass: the END search never rescans text it already proved END-free."""
    out: list[str] = []
    pos = 0
    length = len(text)
    cached_end: Optional[re.Match[str]] = None
    no_end_after = length + 1  # no END marker exists at/after this offset
    while pos < length:
        m = _PEM_BEGIN.search(text, pos)
        if not m:
            break
        out.append(text[pos : m.start()])
        end_pos = length
        if m.end() < no_end_after:
            if cached_end is None or cached_end.start() < m.end():
                cached_end = _PEM_END.search(text, m.end())
                if cached_end is None:
                    no_end_after = m.end()
            if cached_end is not None:
                end_pos = cached_end.end()
        kinds["private_key"] = kinds.get("private_key", 0) + 1
        out.append(_marker("private_key"))
        pos = end_pos
    out.append(text[pos:])
    return "".join(out)


def _auth_repl(m: re.Match[str]) -> Optional[str]:
    value = m.group(2).strip()
    scheme = ""
    scheme_match = _AUTH_SCHEME.match(value)
    if scheme_match:
        scheme, value = scheme_match.group(1), scheme_match.group(2).strip()
    if _is_placeholder(value):
        return None
    return f"{m.group(1)}{scheme}{_marker('bearer_token')}"


def _cli_repl(m: re.Match[str]) -> Optional[str]:
    value = m.group(3)
    if _is_placeholder(value):
        return None
    if "=" not in m.group("sep"):  # "--password when prompted" is prose, not a credential
        if len(value) < 8 or not any(c.isdigit() or not c.isalpha() for c in value):
            return None
    return f"{m.group(1)}{_marker('secret_assignment')}"


def _assign_repl(m: re.Match[str]) -> Optional[str]:
    quote = '"' if m.group("dq") is not None else "'" if m.group("sq") is not None else ""
    value = m.group("dq") or m.group("sq") or m.group("uv") or ""
    tail = ""
    if not quote:  # keep unbalanced closing brackets of an enclosing call/literal
        for closer, opener in ((")", "("), ("]", "["), ("}", "{")):
            while value.endswith(closer) and value.count(closer) > value.count(opener):
                value, tail = value[:-1], closer + tail
    if _is_placeholder(value):
        return None
    prefix = m.group(0)[: m.start("sep") - m.start() + len(m.group("sep"))]
    marker = _marker("secret_assignment")
    return f"{prefix}{quote}{marker}{quote}" if quote else f"{prefix}{marker}{tail}"


def redact(text: str) -> RedactionResult:
    """Return ``text`` with secret-shaped substrings replaced; never raises."""
    if not isinstance(text, str):
        try:
            return RedactionResult(str(text), 0, {})
        except Exception:  # noqa: BLE001 - arbitrary __str__ implementations must not break us
            return RedactionResult("", 0, {})

    kinds: dict[str, int] = {}
    try:
        current = _redact_pem(text, kinds)
        for pattern, kind in (
            (_AWS_ACCESS_KEY, "aws_access_key"),
            (_GITHUB_CLASSIC, "github_token"),
            (_GITHUB_PAT, "github_token"),
            (_GITLAB, "gitlab_token"),
            (_NPM, "npm_token"),
            (_PYPI, "pypi_token"),
            (_HUGGINGFACE, "huggingface_token"),
            (_SLACK, "slack_token"),
            (_ANTHROPIC, "anthropic_key"),
            (_OPENAI, "openai_key"),
            (_GOOGLE, "google_api_key"),
            (_STRIPE, "stripe_key"),
            (_JWT, "jwt"),
        ):
            current = _sub_count(current, pattern, kind, kinds, _simple_marker(kind))
        current = _sub_count(
            current,
            _AWS_SECRET,
            "aws_secret",
            kinds,
            lambda m: f"{m.group(1)}{_marker('aws_secret')}",
        )
        current = _sub_count(current, _AUTH_HEADER, "bearer_token", kinds, _auth_repl)
        current = _sub_count(
            current,
            _BEARER_WORD,
            "bearer_token",
            kinds,
            lambda m: (
                None if _is_placeholder(m.group(2)) else f"{m.group(1)}{_marker('bearer_token')}"
            ),
        )
        current = _sub_count(
            current,
            _COOKIE,
            "cookie",
            kinds,
            lambda m: None if _is_placeholder(m.group(2)) else f"{m.group(1)}{_marker('cookie')}",
        )
        current = _sub_count(
            current,
            _URL_PASSWORD,
            "url_password",
            kinds,
            lambda m: (
                None
                if _is_placeholder(m.group(3))
                else f"{m.group(1)}{m.group(2)}:{_marker('url_password')}@"
            ),
        )
        current = _sub_count(current, _ASSIGN, "secret_assignment", kinds, _assign_repl)
        current = _sub_count(current, _CLI_FLAG, "secret_assignment", kinds, _cli_repl)
        current = _sub_count(
            current,
            _SAS_SIG,
            "sas_signature",
            kinds,
            lambda m: f"{m.group(1)}{_marker('sas_signature')}",
        )
    except (RecursionError, MemoryError):  # never raise: fail closed, nothing is sent through
        return RedactionResult(_marker("unprocessable"), 1, {"unprocessable": 1})

    return RedactionResult(current, sum(kinds.values()), kinds)


_SECRET_KEY = re.compile(r"(?i)^" + _KEY_NAME + r"$")


def redact_value(key: object, value: str) -> RedactionResult:
    """Redact ``value`` of a structured field; the field name is context (``password`` etc.).

    A string stored under a secret-like key is replaced as a whole unless it is a placeholder;
    any other value goes through :func:`redact`.
    """
    if isinstance(key, str) and len(key) <= 120 and _SECRET_KEY.match(key):
        if isinstance(value, str) and len(value) >= 4 and not _is_placeholder(value):
            return RedactionResult(_marker("secret_assignment"), 1, {"secret_assignment": 1})
    return redact(value)


def redact_many(texts: Iterable[str]) -> tuple[list[str], int]:
    """Redact each string; return redacted texts and the total replacement count."""
    out: list[str] = []
    total = 0
    for item in texts:
        result = redact(item)
        out.append(result.text)
        total += result.count
    return out, total
