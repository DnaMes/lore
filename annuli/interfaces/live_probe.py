import argparse
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeCase:
    path: str
    expected_status: int
    expected_fragment: Optional[str] = None


PROBE_CASES: Tuple[ProbeCase, ...] = (
    ProbeCase("/session/bad;id", 400),
    ProbeCase("/session/session-id-does-not-exist-xyz", 404),
    ProbeCase("/thread/bad;id", 400),
    ProbeCase("/thread/thread-id-does-not-exist-xyz", 404),
    ProbeCase("/export/bad;id", 400),
    ProbeCase("/export/session-id-does-not-exist-xyz", 404),
    ProbeCase("/api/search?q=a", 200, "[]"),
    ProbeCase("/api/search?q=ok%3Bdrop", 400, "Invalid search query"),
    ProbeCase("/api/search?q=ok&tool=bad%3Btool", 400, "Invalid tool parameter"),
    ProbeCase(
        "/api/search?q=ok&project=bad%3Bproject",
        400,
        "Invalid project parameter",
    ),
)


def build_basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def is_gateway_auth_block(status_code: int, headers: dict) -> bool:
    if status_code != 401:
        return False
    header_value = ""
    for key, value in headers.items():
        if str(key).lower() == "www-authenticate":
            header_value = str(value).lower()
            break
    return "basic" in header_value and "traefik" in header_value


def evaluate_probe_result(case: ProbeCase, status_code: int, body_text: str) -> Tuple[bool, str]:
    if status_code != case.expected_status:
        return (
            False,
            f"status mismatch (expected {case.expected_status}, got {status_code})",
        )
    if case.expected_fragment and case.expected_fragment not in body_text:
        return (
            False,
            f"body missing fragment: {case.expected_fragment!r}",
        )
    return True, "ok"


def run_probe_case(
    base_url: str,
    case: ProbeCase,
    auth_header: Optional[str],
    timeout_seconds: int,
    timeout_retries: int,
) -> dict:
    target = urljoin(base_url.rstrip("/") + "/", case.path.lstrip("/"))
    headers = {"Accept": "application/json, text/html;q=0.9"}
    if auth_header:
        headers["Authorization"] = auth_header
    request = Request(target, headers=headers, method="GET")

    status_code = 0
    body_text = ""
    response_headers = {}
    error = ""

    attempts = timeout_retries + 1
    for attempt_index in range(attempts):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.getcode()
                response_headers = dict(response.headers.items())
                body_text = response.read().decode("utf-8", errors="replace")
                error = ""
                break
        except HTTPError as exc:
            status_code = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            body_text = exc.read().decode("utf-8", errors="replace")
            error = ""
            break
        except URLError as exc:
            error = str(exc)
            break
        except TimeoutError:
            error = "request timed out"
            if attempt_index >= timeout_retries:
                break
        except OSError as exc:
            error = str(exc)
            break

    blocked_by_gateway = is_gateway_auth_block(status_code, response_headers)

    if error == "request timed out" and case.expected_fragment is None:
        head_request = Request(target, headers=headers, method="HEAD")
        try:
            with urlopen(head_request, timeout=timeout_seconds) as response:
                status_code = response.getcode()
                response_headers = dict(response.headers.items())
                body_text = ""
                error = ""
        except HTTPError as exc:
            status_code = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            body_text = ""
            error = ""
        except Exception as exc:
            # The HEAD fallback failed — keep the GET attempt's error state
            # so downstream evaluates this probe as still-failed (#123).
            logger.debug("HEAD fallback probe failed for %s: %s", target, exc)

    blocked_by_gateway = is_gateway_auth_block(status_code, response_headers)
    passed = False
    detail = error or "request failed"
    if not error and not blocked_by_gateway:
        passed, detail = evaluate_probe_result(case, status_code, body_text)
    elif blocked_by_gateway:
        detail = "blocked by upstream basic auth"

    return {
        "path": case.path,
        "url": target,
        "expected_status": case.expected_status,
        "status": status_code,
        "passed": passed,
        "blocked": blocked_by_gateway,
        "detail": detail,
    }


def fetch_build_info(base_url: str, auth_header: Optional[str], timeout_seconds: int) -> dict:
    target = urljoin(base_url.rstrip("/") + "/", "api/build-info")
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    request = Request(target, headers=headers, method="GET")

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.getcode()
            body_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status_code = exc.code
        body_text = exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "url": target,
            "status": 0,
            "detail": str(exc) or "request failed",
            "revision": None,
            "module": None,
        }

    revision = None
    module_name = None
    detail = "ok"
    try:
        payload = json.loads(body_text)
        if isinstance(payload, dict):
            revision = payload.get("revision")
            module_name = payload.get("module")
    except json.JSONDecodeError:
        detail = "invalid json body"

    return {
        "url": target,
        "status": status_code,
        "detail": detail,
        "revision": revision,
        "module": module_name,
    }


def run_probe_matrix(
    base_url: str,
    cases: Iterable[ProbeCase] = PROBE_CASES,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_seconds: int = 10,
    timeout_retries: int = 1,
) -> dict:
    auth_header = None
    if username and password:
        auth_header = build_basic_auth_header(username, password)

    build_info = fetch_build_info(
        base_url=base_url,
        auth_header=auth_header,
        timeout_seconds=timeout_seconds,
    )

    results: List[dict] = []
    for case in cases:
        results.append(
            run_probe_case(
                base_url,
                case,
                auth_header,
                timeout_seconds,
                timeout_retries,
            )
        )

    blocked = any(result["blocked"] for result in results)
    passed = all(result["passed"] for result in results)
    failures = [result for result in results if not result["passed"] and not result["blocked"]]

    return {
        "base_url": base_url,
        "build_info": build_info,
        "results": results,
        "blocked": blocked,
        "passed": passed,
        "failure_count": len(failures),
    }


def _resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.password:
        return args.password
    if args.password_env:
        return os.environ.get(args.password_env)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run lore web security probe matrix against a base URL."
    )
    parser.add_argument("--base-url", required=True, help="Base URL to probe")
    parser.add_argument("--user", default="", help="Basic auth username")
    parser.add_argument("--password", default="", help="Basic auth password")
    parser.add_argument(
        "--password-env",
        default="LORE_WEB_PROBE_PASSWORD",
        help="Env var name for basic auth password",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--timeout-retries",
        type=int,
        default=1,
        help="Retries to perform after a timeout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output instead of line-by-line text",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    password = _resolve_password(args)
    username = args.user or None
    if username and not password:
        print("error: --user requires --password or --password-env with a value")
        return 2

    result = run_probe_matrix(
        base_url=args.base_url,
        username=username,
        password=password,
        timeout_seconds=args.timeout_seconds,
        timeout_retries=max(0, args.timeout_retries),
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Base URL: {result['base_url']}")
        build_info = result.get("build_info", {})
        print(
            "Build info: "
            f"status={build_info.get('status', 0)} "
            f"revision={build_info.get('revision') or 'unknown'} "
            f"module={build_info.get('module') or 'unknown'}"
        )
        for row in result["results"]:
            status = "PASS" if row["passed"] else ("BLOCKED" if row["blocked"] else "FAIL")
            print(
                f"[{status}] {row['path']} -> {row['status']} "
                f"(expected {row['expected_status']}) - {row['detail']}"
            )

    if result["blocked"]:
        return 3
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
