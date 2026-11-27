import base64
from email.message import Message

from annuli.interfaces.live_probe import (
    ProbeCase,
    build_basic_auth_header,
    evaluate_probe_result,
    fetch_build_info,
    is_gateway_auth_block,
    run_probe_case,
    run_probe_matrix,
)


def test_build_basic_auth_header_encodes_expected_value():
    header = build_basic_auth_header("alice", "s3cr3t")
    expected = base64.b64encode(b"alice:s3cr3t").decode("ascii")

    assert header == f"Basic {expected}"


def test_is_gateway_auth_block_requires_401_traefik_basic_header():
    assert not is_gateway_auth_block(200, {"WWW-Authenticate": 'Basic realm="traefik"'})
    assert not is_gateway_auth_block(401, {"WWW-Authenticate": 'Bearer realm="traefik"'})
    assert is_gateway_auth_block(401, {"WWW-Authenticate": 'Basic realm="traefik"'})
    assert is_gateway_auth_block(401, {"Www-Authenticate": 'Basic realm="traefik"'})


def test_evaluate_probe_result_accepts_expected_status_and_fragment():
    case = ProbeCase("/api/search?q=a", 200, "[]")

    passed, detail = evaluate_probe_result(case, 200, "[]")

    assert passed is True
    assert detail == "ok"


def test_evaluate_probe_result_rejects_missing_fragment():
    case = ProbeCase("/api/search?q=ok%3Bdrop", 400, "Invalid search query")

    passed, detail = evaluate_probe_result(case, 400, "different body")

    assert passed is False
    assert "body missing fragment" in detail


def test_run_probe_case_handles_timeout_without_crashing(monkeypatch):
    case = ProbeCase("/session/bad;id", 400)

    def _boom(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr("annuli.interfaces.live_probe.urlopen", _boom)

    result = run_probe_case(
        "https://example.com",
        case,
        auth_header=None,
        timeout_seconds=1,
        timeout_retries=0,
    )

    assert result["passed"] is False
    assert result["blocked"] is False
    assert result["detail"] == "request timed out"


def test_run_probe_case_uses_head_fallback_after_timeout(monkeypatch):
    case = ProbeCase("/export/session-id-does-not-exist-xyz", 404)

    class _FakeResponse:
        def __init__(self, code):
            self._code = code
            self.headers = Message()

        def getcode(self):
            return self._code

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    def _urlopen(request, timeout):
        if request.get_method() == "GET":
            raise TimeoutError
        return _FakeResponse(404)

    monkeypatch.setattr("annuli.interfaces.live_probe.urlopen", _urlopen)

    result = run_probe_case(
        "https://example.com",
        case,
        auth_header=None,
        timeout_seconds=1,
        timeout_retries=0,
    )

    assert result["passed"] is True
    assert result["detail"] == "ok"


def test_fetch_build_info_parses_revision_and_module(monkeypatch):
    class _FakeResponse:
        def __init__(self):
            self.headers = Message()

        def getcode(self):
            return 200

        def read(self):
            return b'{"revision":"rev-1","module":"annuli.interfaces.web"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "annuli.interfaces.live_probe.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(),
    )

    result = fetch_build_info(
        base_url="https://example.com",
        auth_header=None,
        timeout_seconds=1,
    )

    assert result["status"] == 200
    assert result["revision"] == "rev-1"
    assert result["module"] == "annuli.interfaces.web"


def test_run_probe_matrix_includes_build_info(monkeypatch):
    monkeypatch.setattr(
        "annuli.interfaces.live_probe.fetch_build_info",
        lambda **_kwargs: {
            "url": "https://example.com/api/build-info",
            "status": 200,
            "detail": "ok",
            "revision": "rev-1",
            "module": "annuli.interfaces.web",
        },
    )
    monkeypatch.setattr(
        "annuli.interfaces.live_probe.run_probe_case",
        lambda *_args, **_kwargs: {
            "path": "/session/bad;id",
            "url": "https://example.com/session/bad;id",
            "expected_status": 400,
            "status": 400,
            "passed": True,
            "blocked": False,
            "detail": "ok",
        },
    )

    result = run_probe_matrix(
        base_url="https://example.com",
        cases=[ProbeCase("/session/bad;id", 400)],
    )

    assert result["passed"] is True
    assert result["build_info"]["revision"] == "rev-1"
