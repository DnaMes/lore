"""Security edge cases beyond the coverage in test_redact.py, test_web_host_allowlist.py
and test_prune_confinement.py. Every case here is new; production behaviour that turns
out to be buggy is pinned with ``xffail(strict=True)`` instead of being fixed here.
"""

from __future__ import annotations

import json
import logging
import random
import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.extractors.base import BaseExtractor
from lore.interfaces import web, web_utils
from lore.utils.redact import redact

# ---------------------------------------------------------------------------
# (a) redact() edge cases
# ---------------------------------------------------------------------------


def test_crlf_line_endings_do_not_break_pem_redaction():
    """A PEM block with \\r\\n line endings (Windows logs) must still be redacted whole."""
    pem = "x\r\n-----BEGIN RSA PRIVATE KEY-----\r\nAAAA\r\n-----END RSA PRIVATE KEY-----\r\ny"
    result = redact(pem)
    assert "BEGIN" not in result.text
    assert result.kinds == {"private_key": 1}


def test_secrets_on_separate_lines_are_each_redacted():
    text = "\n".join(["AKIA" + "Q" * 16, "ghp_" + "a" * 36, "sk-ant-" + "k" * 20, "tail"])
    result = redact(text)
    assert result.kinds == {"aws_access_key": 1, "github_token": 1, "anthropic_key": 1}
    assert result.text == "\n".join(
        ["[REDACTED:aws_access_key]", "[REDACTED:github_token]", "[REDACTED:anthropic_key]", "tail"]
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: redact() leaves an assignment value unredacted when it sits on the line after "
        "the key (valid YAML block scalar); the secret leaks with count 0"
    ),
)
def test_assignment_value_on_next_line_is_redacted():
    result = redact("db_password:\n    hunter2hunter2\n")
    assert "hunter2hunter2" not in result.text
    assert result.count == 1


@pytest.mark.parametrize(
    "sample",
    [
        "（AKIAABCDEFGHIJKLMNOP）",  # fullwidth parens around an AWS access key
        "„xoxb-ABCDEFGHIJKL” token",  # low/high quotes around a Slack token
        "password=«hunter2hunter2»",  # guillemets around an unquoted value
        "password=“hunter2hunter2”",  # typographic quotes around a quoted value
    ],
)
def test_unicode_punctuation_around_secrets_is_redacted(sample: str):
    result = redact(sample)
    assert result.count == 1, sample
    for kind in ("aws_access_key", "slack_token", "secret_assignment"):
        assert kind not in result.kinds or result.kinds[kind] == 1
    assert "hunter2" not in result.text
    assert "AKIAABCDEFGHIJKLMNOP" not in result.text
    assert "xoxb-ABCDEFGHIJKL" not in result.text


def test_nested_json_string_with_token_is_redacted():
    result = redact('{"creds": {"api_key": "sk-abcdefghijklmnopqrst", "note": "ok"}}')
    assert result.text == '{"creds": {"api_key": "[REDACTED:openai_key]", "note": "ok"}}'


def test_json_escaped_unicode_secret_is_redacted():
    """A value spelled with backslash-u escapes is still a secret inside the JSON string."""
    result = redact('{"password": "\\u0073ecret-pw-value"}')
    assert result.text == '{"password": "[REDACTED:secret_assignment]"}'


def test_url_credentials_in_json_and_multiple_urls_are_redacted():
    result = redact('{"login": "https://ci:buildpass12@build.example"}')
    assert result.kinds == {"url_password": 1}
    assert "buildpass12" not in result.text
    pair = "https://alice:secret111@a.example https://bob:secret222@b.example"
    result = redact(pair)
    assert result.kinds == {"url_password": 2}
    assert "secret111" not in result.text and "secret222" not in result.text
    result = redact("ftp://backup:correcthorse@files.example/dump")
    assert result.kinds == {"url_password": 1}


def test_url_placeholder_password_is_left_alone():
    text = "https://ci:${PASSWORD}@build.example/hook"
    result = redact(text)
    assert result.text == text
    assert result.count == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: a percent-encoded URL password starting with % (e.g. %70ass%40word) is treated "
        "as a placeholder by _is_placeholder, so the credentials leak unredacted"
    ),
)
def test_percent_encoded_url_password_is_redacted():
    result = redact("https://user:%70ass%40word@host.example/path")
    assert "%70ass%40word" not in result.text
    assert result.kinds == {"url_password": 1}


def test_one_megabyte_input_with_scattered_tokens_under_5s():
    block = (
        '{"note": "' + "a" * 800 + '"}\n'
        "https://ci:" + "p" * 12 + "@build.example/hook " + "AKIAABCDEFGHIJKLMNOP\n"
    )
    repeats = 1_050_000 // len(block) + 1
    payload = block * repeats
    assert len(payload) >= 1_000_000
    start = time.perf_counter()
    result = redact(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"too slow ({elapsed:.2f}s) for len={len(payload)}"
    assert result.kinds == {"url_password": repeats, "aws_access_key": repeats}
    assert result.count == 2 * repeats


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: a token whose trailing \\b is blocked by an adjacent token leaks after one pass "
        "(npm_<36 alnum>xoxb-... only redacts the xoxb token; on a second pass the boundary "
        "has appeared and the npm token is redacted too), so redact is not idempotent"
    ),
)
def test_redact_is_idempotent_on_seeded_token_composites():
    # Minimal deterministic repro first: a full npm token survives one pass here.
    minimal = "npm_" + "N" * 36 + "xoxb-" + "A" * 12
    once = redact(minimal)
    assert "npm_" + "N" * 36 not in once.text
    assert redact(once.text).text == once.text

    # 50 hypothesis-free randomized-but-seeded composites of token-shaped pieces.
    rng = random.Random(8_025_501)
    token_pieces = [
        "AKIA" + "Q" * 16,
        "ghp_" + "a" * 36,
        "xoxb-" + "A" * 12,
        "npm_" + "N" * 36,
        "sk-ant-" + "k" * 20,
        "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + ".eyJ" + "c" * 12,
        "https://ci:" + "p" * 12 + "@build.example",
        "password=hunter2hunter2",
        "Authorization: Bearer " + "z" * 24,
        "Cookie: sid=abcdefghijklmnop",
    ]
    filler_chars = "abcdefghijklmnopqrstuvwxyz0123456789 ,.:;-_/\"'@=()[]{}\n«»—"
    for _ in range(50):
        parts = []
        for _ in range(rng.randint(1, 12)):
            if rng.random() < 0.45:
                parts.append(rng.choice(token_pieces))
            else:
                parts.append("".join(rng.choice(filler_chars) for _ in range(rng.randint(0, 60))))
        text = "".join(parts)
        once = redact(text)
        twice = redact(once.text)
        assert twice.text == once.text, text
        assert twice.count == 0, text


# ---------------------------------------------------------------------------
# (b) Host / Origin allowlist edge cases
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    monkeypatch.delenv("LORE_PUBLIC_ORIGINS", raising=False)
    monkeypatch.delenv("LORE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("LORE_WEB_HOST", raising=False)
    monkeypatch.setattr(web, "_BIND_HOST", "")
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_uppercase_allowed_hosts_entry_matches_lowercase_request(client, monkeypatch):
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", " Lore.Example.COM ")
    assert client.get("/api/health", headers={"Host": "lore.example.com"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 400


def test_trailing_dot_host_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "localhost."}).status_code == 400


def test_trailing_dot_entry_never_matches_dotless_host(monkeypatch):
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "lore.example.")
    assert web_utils.host_header_allowed("lore.example") is False
    assert web_utils.host_header_allowed("lore.example.") is True


def test_bare_ipv6_literal_without_brackets_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "2001:db8::1"}).status_code == 400
    assert web_utils._split_host("2001:db8::1") is None


def test_ipv6_origin_parsing_and_allowlist():
    assert web_utils.parse_origin("http://[::1]:8080") == ("http", "::1", 8080)
    assert web_utils.normalize_origin("http", "::1", 8080) == "http://[::1]:8080"
    assert web_utils.normalize_origin("https", "::1", 443) == "https://[::1]"
    assert web_utils.host_header_allowed("[::1]:8080") is True


def test_userinfo_in_host_header_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "user@localhost:5000"}).status_code == 400


def test_userinfo_in_origin_is_not_an_origin():
    assert web_utils.parse_origin("http://user:pass@localhost:5000") is None


def test_userinfo_entry_in_allowed_hosts_is_ignored_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "user@lore.example")
    caplog.set_level(logging.WARNING, logger="lore.interfaces.web_utils")
    assert web_utils.host_header_allowed("lore.example:5000") is False
    assert any("Ignoring malformed host entry" in r.getMessage() for r in caplog.records)


def test_port_zero_and_overflow_ports_are_never_allowed_hosts():
    assert web_utils.host_header_allowed("localhost:0") is False
    assert web_utils.host_header_allowed("localhost:65536") is False
    assert web_utils.host_header_allowed("[::1]:65536") is False


def test_invalid_port_entry_in_allowed_hosts_is_ignored_with_warning(client, monkeypatch, caplog):
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "lore.example:0")
    caplog.set_level(logging.WARNING, logger="lore.interfaces.web_utils")
    assert client.get("/api/health", headers={"Host": "lore.example:5000"}).status_code == 400
    assert any("Ignoring malformed host entry" in r.getMessage() for r in caplog.records)


def test_duplicate_host_headers_are_rejected(client):
    resp = client.get(
        "/api/health",
        headers=[("Host", "localhost:5000"), ("Host", "attacker.example:5000")],
    )
    assert resp.status_code == 400


def test_comma_joined_host_header_is_rejected(client):
    resp = client.get("/api/health", headers={"Host": "localhost:5000, attacker.example"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# (c) prune confinement edge cases
# ---------------------------------------------------------------------------

_EXPORT_NAME = "2026-01-01_title_edge-sess.md"


def _session(sid: str, prompts: int = 1) -> UnifiedSession:
    messages = [
        UnifiedMessage(role=Role.USER, content="ask", timestamp=datetime(2026, 1, 1)),
        UnifiedMessage(role=Role.ASSISTANT, content="ans", timestamp=datetime(2026, 1, 1)),
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/proj",
        messages=messages * prompts,
    )


class _EmptyExtractor(BaseExtractor):
    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        return iter(())


def _prune_args(output_dir: Path) -> Namespace:
    return Namespace(
        output_dir=str(output_dir), max_prompts=3, dry_run=False, include_automated=False
    )


def _archive_with_candidate(tmp_path: Path, target: Path) -> Path:
    """Index one low-prompt session whose export_path points at ``target``."""
    archive = tmp_path / "lore"
    archive.mkdir(exist_ok=True)
    IndexBuilder(archive).build_index([_session("edge-sess")], {})
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "edge-sess":
            entry["export_path"] = str(target)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return archive


def _run_prune(monkeypatch: pytest.MonkeyPatch, archive: Path) -> str:
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_EmptyExtractor()])
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        lore_cli.cmd_prune(_prune_args(archive))
    return err.getvalue()


def test_prune_refuses_dotdot_escape_beyond_archive_root(tmp_path, monkeypatch):
    archive = tmp_path / "lore"
    (archive / "projects" / "p" / "tool").mkdir(parents=True)
    escaped = tmp_path / "escaped.md"
    escaped.write_text("keep me", encoding="utf-8")
    # tool -> p -> projects -> lore -> tmp_path: lands outside the archive root.
    target = archive / "projects" / "p" / "tool" / ".." / ".." / ".." / ".." / "escaped.md"
    _archive_with_candidate(tmp_path, target)

    err = _run_prune(monkeypatch, archive)

    assert escaped.exists()
    assert escaped.read_text(encoding="utf-8") == "keep me"
    assert "refusing to delete" in err


def test_prune_deletes_export_when_dotdot_resolves_inside_layout(tmp_path, monkeypatch):
    """A ``..`` segment that normalises to the exporter layout is still deletable."""
    archive = tmp_path / "lore"
    export = archive / "projects" / "p" / "tool" / _EXPORT_NAME
    export.parent.mkdir(parents=True)
    export.write_text("# export", encoding="utf-8")
    (export.parent / "sub").mkdir()
    weird = archive / "projects" / "p" / "tool" / "sub" / ".." / _EXPORT_NAME
    _archive_with_candidate(tmp_path, weird)

    err = _run_prune(monkeypatch, archive)

    assert not export.exists()
    assert "refusing to delete" not in err


def test_prune_refuses_prefix_sibling_directory(tmp_path, monkeypatch):
    """``<root>-evil`` shares a string prefix with the root but is a different tree."""
    sibling = tmp_path / "lore-evil" / "projects" / "p" / "tool" / _EXPORT_NAME
    sibling.parent.mkdir(parents=True)
    sibling.write_text("evil keep me", encoding="utf-8")
    _archive_with_candidate(tmp_path, sibling)

    err = _run_prune(monkeypatch, tmp_path / "lore")

    assert sibling.exists()
    assert sibling.read_text(encoding="utf-8") == "evil keep me"
    assert "refusing to delete" in err


def test_prune_refuses_symlinked_parent_directory_escape(tmp_path, monkeypatch):
    """A symlinked directory inside projects/ must not make outside files deletable."""
    archive = tmp_path / "lore"
    archive.mkdir()
    (archive / "projects").mkdir()
    IndexBuilder(archive).build_index([_session("edge-sess")], {})
    outside = tmp_path / "outside" / "p" / "tool"
    outside.mkdir(parents=True)
    real = outside / _EXPORT_NAME
    real.write_text("keep me", encoding="utf-8")
    (archive / "projects" / "p").symlink_to(tmp_path / "outside" / "p")
    through = archive / "projects" / "p" / "tool" / _EXPORT_NAME
    index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
    for entry in index["sessions"]:
        if entry["id"] == "edge-sess":
            entry["export_path"] = str(through)
            entry["prompts"] = 1
    (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")

    err = _run_prune(monkeypatch, archive)

    assert real.exists()
    assert real.read_text(encoding="utf-8") == "keep me"
    assert (archive / "projects" / "p").is_symlink()
    assert "refusing to delete" in err
