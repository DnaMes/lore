"""Tests for CSP nonce-based inline script/style hardening (issue #7).

Verifies that:
- Every HTML response sets a Content-Security-Policy header with 'nonce-<value>'
- script-src stays nonce-only (no 'unsafe-inline')
- style-src keeps 'unsafe-inline' for the Tailwind JIT runtime sheet (#19)
- All inline <script> and <style> tags carry the matching nonce attribute
- The nonce changes between requests (per-request entropy)
- get_csp_nonce() returns a safe fallback outside a request context
"""

import re

from annuli.interfaces import web

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_nonce_from_csp(csp: str) -> str | None:
    """Pull the nonce value out of a CSP header like ``'nonce-abc123'``."""
    match = re.search(r"'nonce-([^']+)'", csp)
    return match.group(1) if match else None


def _inline_script_nonces(html: str) -> list[str]:
    """Return nonce attribute values from all inline <script nonce="..."> tags.

    External <script src="..."> tags are excluded because they don't need nonces.
    """
    # Matches <script nonce="VALUE"> but not <script src="...">
    return re.findall(r'<script\s+nonce="([^"]+)"', html)


def _inline_style_nonces(html: str) -> list[str]:
    return re.findall(r'<style\s+nonce="([^"]+)"', html)


# ---------------------------------------------------------------------------
# CSP header structure
# ---------------------------------------------------------------------------


class TestCspHeader:
    def test_no_unsafe_inline_in_script_src(self):
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        script_src = next((d for d in csp.split(";") if "script-src" in d), "")
        assert "unsafe-inline" not in script_src, f"script-src must stay nonce-only: {script_src}"

    def test_style_src_keeps_unsafe_inline_for_tailwind_jit(self):
        """The vendored Tailwind JIT injects an un-nonced runtime <style>.

        script-src stays strict (nonce-only); style-src must allow
        'unsafe-inline' so that injected sheet is not blocked (issue #19).
        """
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        style_src = next((d for d in csp.split(";") if "style-src" in d), "")
        assert "unsafe-inline" in style_src, f"style-src needs unsafe-inline: {style_src}"

    def test_no_cdn_origins_in_csp(self):
        """Issue #19: all assets are vendored — no third-party origins allowed."""
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        for origin in (
            "cdn.tailwindcss.com",
            "cdnjs.cloudflare.com",
            "cdn.jsdelivr.net",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
        ):
            assert origin not in csp, f"CDN origin {origin} still in CSP: {csp}"

    def test_nonce_present_in_csp(self):
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        assert _extract_nonce_from_csp(csp) is not None, f"No nonce found in CSP: {csp}"

    def test_nonce_in_script_src_only(self):
        """script-src carries the nonce; style-src must NOT.

        A nonce in style-src makes CSP3 ignore 'unsafe-inline', which would
        block every Tailwind-injected inline style and leave the UI unstyled.
        """
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        nonce = _extract_nonce_from_csp(csp)
        assert nonce, f"No nonce in CSP: {csp}"
        script_src = next((d for d in csp.split(";") if "script-src" in d), "")
        style_src = next((d for d in csp.split(";") if "style-src" in d), "")
        assert f"'nonce-{nonce}'" in script_src, f"nonce missing from script-src: {script_src}"
        assert "nonce-" not in style_src, (
            f"style-src must NOT carry a nonce (would disable unsafe-inline): {style_src}"
        )

    def test_hardened_directives_still_present(self):
        """Regression guard: existing strict directives must survive the nonce change."""
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp


# ---------------------------------------------------------------------------
# Nonce rotation
# ---------------------------------------------------------------------------


class TestNonceRotation:
    def test_nonce_changes_between_requests(self):
        with web.app.test_client() as client:
            r1 = client.get("/")
            r2 = client.get("/")
        csp1 = r1.headers.get("Content-Security-Policy", "")
        csp2 = r2.headers.get("Content-Security-Policy", "")
        nonce1 = _extract_nonce_from_csp(csp1)
        nonce2 = _extract_nonce_from_csp(csp2)
        assert nonce1 and nonce2, "Nonce missing from one or both responses"
        assert nonce1 != nonce2, "Nonce must be unique per request"

    def test_nonce_is_urlsafe_base64(self):
        """token_urlsafe output must only contain URL-safe characters."""
        with web.app.test_client() as client:
            response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        nonce = _extract_nonce_from_csp(csp)
        assert nonce, "No nonce found"
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", nonce), f"Nonce is not URL-safe: {nonce!r}"


# ---------------------------------------------------------------------------
# HTML — nonce attribute on inline elements
# ---------------------------------------------------------------------------


class TestInlineNonceAttributes:
    def _get_html_and_nonce(self, path: str = "/") -> tuple[str, str]:
        with web.app.test_client() as client:
            response = client.get(path)
        html = response.get_data(as_text=True)
        csp = response.headers.get("Content-Security-Policy", "")
        nonce = _extract_nonce_from_csp(csp)
        return html, nonce or ""

    def test_all_inline_scripts_carry_nonce(self):
        html, nonce = self._get_html_and_nonce("/")
        assert nonce, "No nonce in CSP"
        found = _inline_script_nonces(html)
        assert len(found) > 0, "No <script nonce=...> tags found in response"
        for tag_nonce in found:
            assert tag_nonce == nonce, (
                f"Inline script has nonce {tag_nonce!r} but CSP nonce is {nonce!r}"
            )

    def test_all_inline_styles_carry_nonce(self):
        html, nonce = self._get_html_and_nonce("/")
        assert nonce, "No nonce in CSP"
        found = _inline_style_nonces(html)
        assert len(found) > 0, "No <style nonce=...> tags found in response"
        for tag_nonce in found:
            assert tag_nonce == nonce, (
                f"Inline style has nonce {tag_nonce!r} but CSP nonce is {nonce!r}"
            )

    def test_no_unnnonced_inline_scripts(self):
        """Ensure there are no bare <script> tags (without a nonce or src)."""
        html, _ = self._get_html_and_nonce("/")
        # Match <script> tags that have neither nonce nor src attribute
        bare = re.findall(r"<script(?!\s+(?:nonce|src)\b)[^>]*>", html, re.IGNORECASE)
        assert not bare, f"Found un-nonced inline <script> tags: {bare}"

    def test_no_unnnonced_inline_styles(self):
        """Ensure there are no bare <style> tags without a nonce."""
        html, _ = self._get_html_and_nonce("/")
        bare = re.findall(r"<style(?!\s+nonce\b)[^>]*>", html, re.IGNORECASE)
        assert not bare, f"Found un-nonced inline <style> tags: {bare}"

    def test_html_nonce_matches_csp_nonce(self):
        """The nonce embedded in HTML tags must exactly equal the CSP nonce."""
        html, nonce = self._get_html_and_nonce("/")
        assert nonce
        all_html_nonces = _inline_script_nonces(html) + _inline_style_nonces(html)
        assert all_html_nonces, "No nonced inline elements found at all"
        for tag_nonce in all_html_nonces:
            assert tag_nonce == nonce


# ---------------------------------------------------------------------------
# get_csp_nonce() fallback
# ---------------------------------------------------------------------------


class TestGetCspNonceFallback:
    def test_fallback_outside_request_context(self):
        """get_csp_nonce() must return an empty string outside a request context."""
        result = web.get_csp_nonce()
        assert result == "", f"Expected empty string fallback, got {result!r}"

    def test_nonce_within_request_context(self):
        """Inside a real request, get_csp_nonce() returns a non-empty string."""
        with web.app.test_request_context("/"):
            web.app.preprocess_request()
            nonce = web.get_csp_nonce()
        assert nonce, "Expected non-empty nonce inside request context"
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", nonce)
