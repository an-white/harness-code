"""Tests for web tools, consent, SSRF guardrails, and audit (no network)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.loader import PermissionRule
from app.config import get_settings
from app.tools import make_web_tools, sanitize_query, web_access_decision, write_web_audit
from app.tools.policy import PermissionPolicy


class _FakeResponse:
    def __init__(self, body=b"hello", status=200, headers=None, url="https://example.com/"):
        self._io = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Type": "text/plain; charset=utf-8"}
        self._url = url

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self._url


class _DummySettings:
    web_access = "allow"
    webfetch_timeout = 5
    webfetch_max_bytes = 1024
    webfetch_allowed_domains = []
    websearch_provider = "none"
    websearch_endpoint = None
    websearch_api_key = None


class ConsentAndAuditTest(unittest.TestCase):
    def test_web_access_decision_modes(self):
        s = _DummySettings()
        self.assertTrue(web_access_decision(s, {"decision": "allow"})["allow_webfetch"])
        self.assertFalse(web_access_decision(s, {"decision": "deny"})["allow_webfetch"])
        d = web_access_decision(s, {"decision": "allow_urls", "allowed_urls": ["https://example.com/docs"]})
        self.assertTrue(d["allow_webfetch"])
        self.assertFalse(d["allow_websearch"])
        self.assertTrue(d["urls_only"])

    def test_sanitize_query_rejects_sensitive_inputs(self):
        for bad in [
            "api_key=secret123456",
            "user@example.com issue",
            "see /var/app/project/file.py",
            "C:\\Users\\me\\secret.txt",
            "check https://internal.example.com",
            "```print('x')```",
        ]:
            q, reason = sanitize_query(bad)
            self.assertIsNone(q, bad)
            self.assertTrue(reason)

        q, reason = sanitize_query("python asyncio timeout best practices")
        self.assertEqual(reason, None)
        self.assertIn("python", q)

    def test_write_web_audit_sanitized_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_web_audit(
                tmp,
                {
                    "event": "webfetch",
                    "status": "ok",
                    "domain": "example.com",
                    "sanitized_query": "safe query",
                    "url": "https://example.com/path?secret=1",
                    "reason": "user@example.com /var/x sk-secretSECRET",
                },
                consent={"decision": "allow"},
            )
            self.assertTrue(path and Path(path).is_file())
            data = Path(path).read_text(encoding="utf-8")
            self.assertIn("example.com", data)
            self.assertNotIn("secret=1", data)
            self.assertNotIn("user@example.com", data)
            self.assertNotIn("/var/x", data)
            self.assertNotIn("sk-secretSECRET", data)


class WebToolGuardrailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = PermissionPolicy([
            PermissionRule("webfetch", "*", "allow"),
            PermissionRule("websearch", "*", "allow"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _tools(self, settings, consent):
        return {t.name: t for t in make_web_tools(self.root, self.policy, settings, web_consent=consent)}

    def test_webfetch_denied_without_consent(self):
        s = _DummySettings()
        s.web_access = "ask"
        tools = self._tools(s, {"decision": "deny"})
        out = tools["webfetch"].invoke({"url": "https://example.com"})
        self.assertIn("denied", out.lower())

    def test_webfetch_blocks_private_and_local_hosts(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        for url in [
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://192.168.1.2/",
            "http://169.254.169.254/latest/meta-data/",
        ]:
            out = tools["webfetch"].invoke({"url": url})
            self.assertIn("ERROR", out)

    def test_webfetch_blocks_non_http_schemes_and_credentials(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        self.assertIn("ERROR", tools["webfetch"].invoke({"url": "file:///etc/passwd"}))
        self.assertIn("ERROR", tools["webfetch"].invoke({"url": "https://user:pass@example.com/"}))

    def test_webfetch_blocks_resolved_private_ip(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            out = tools["webfetch"].invoke({"url": "https://example.com/"})
        self.assertIn("ERROR", out)

    def test_webfetch_redirect_revalidated(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        responses = [
            _FakeResponse(b"", status=302, headers={"Location": "http://127.0.0.1/private"}),
            _FakeResponse(b"secret", status=200),
        ]
        calls = []
        def fake_open(req, timeout=None):  # noqa: ARG001
            calls.append(getattr(req, "full_url", req.get_full_url()))
            return responses.pop(0)
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", side_effect=fake_open):
            out = tools["webfetch"].invoke({"url": "https://example.com/start"})
        self.assertIn("ERROR", out)
        self.assertNotIn("secret", out)
        self.assertEqual(len(calls), 1, "must not follow redirect to unvalidated/private target")

    def test_webfetch_success_truncates_and_strips_html(self):
        s = _DummySettings()
        s.webfetch_max_bytes = 80
        tools = self._tools(s, {"decision": "allow"})
        body = b"<html><body><h1>Title</h1><p>" + b"x" * 400 + b"</p></body></html>"
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(body, headers={"Content-Type": "text/html"})):
            out = tools["webfetch"].invoke({"url": "https://example.com/"})
        self.assertIn("Title", out)
        self.assertIn("truncated", out.lower())

    def test_webfetch_urls_only_restricts(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow_urls", "allowed_urls": ["https://allowed.example/docs"]})
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            out = tools["webfetch"].invoke({"url": "https://other.example/"})
        self.assertIn("ERROR", out)
        self.assertIn("allowlist", out.lower())

    def test_websearch_not_configured_or_denied(self):
        s = _DummySettings()
        s.websearch_provider = "none"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        out = tools["websearch"].invoke({"query": "python packaging"})
        self.assertIn("ERROR", out)
        self.assertTrue("not configured" in out.lower() or "denied" in out.lower())

        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.example/search"
        tools = self._tools(s, {"decision": "allow_urls", "allow_websearch": False})
        out = tools["websearch"].invoke({"query": "python packaging"})
        self.assertIn("ERROR", out)

    def test_websearch_rejects_unsafe_query(self):
        s = _DummySettings()
        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.example/search"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            out = tools["websearch"].invoke({"query": "see /var/user/secret.txt"})
        self.assertIn("ERROR", out)
        self.assertIn("rejected", out.lower())

    def test_websearch_provider_success_mocked(self):
        s = _DummySettings()
        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.example/search"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        payload = json.dumps({"results": [{"title": "T", "url": "https://docs.example/", "content": "snippet"}]}).encode()
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(payload, headers={"Content-Type": "application/json"})):
            out = tools["websearch"].invoke({"query": "python http client"})
        self.assertIn("docs.example", out)
        self.assertIn("Provider", out)

    def test_webfetch_blocks_ipv4_mapped_ipv6_literal(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        out = tools["webfetch"].invoke({"url": "http://[::ffff:127.0.0.1]/"})
        self.assertIn("ERROR", out)

    def test_webfetch_blocks_resolved_ipv4_mapped_ipv6(self):
        s = _DummySettings()
        tools = self._tools(s, {"decision": "allow"})
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("::ffff:127.0.0.1", 443, 0, 0))]):
            out = tools["webfetch"].invoke({"url": "https://example.com/"})
        self.assertIn("ERROR", out)

    def test_websearch_query_properly_encoded(self):
        s = _DummySettings()
        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.example/search?lang=en"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        payload = json.dumps({"results": []}).encode()
        captured = {}
        def fake_open(req, timeout=None):  # noqa: ARG001
            captured["url"] = getattr(req, "full_url", req.get_full_url())
            return _FakeResponse(payload, headers={"Content-Type": "application/json"})
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", side_effect=fake_open):
            out = tools["websearch"].invoke({"query": "C++ async/await?"})
        self.assertNotIn("ERROR", out)
        url = captured.get("url", "")
        self.assertIn("lang=en", url)
        self.assertIn("format=json", url)
        self.assertIn("q=C%2B%2B+async%2Fawait%3F", url)
        self.assertNotIn("C++ async/await?", url)

    def test_websearch_endpoint_exempt_from_domain_allowlist(self):
        s = _DummySettings()
        s.webfetch_allowed_domains = ["allowed.example"]
        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.other.example/search"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        payload = json.dumps({"results": [{"title": "T", "url": "https://docs.example/", "content": "snippet"}]}).encode()
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", return_value=_FakeResponse(payload, headers={"Content-Type": "application/json"})):
            out = tools["websearch"].invoke({"query": "python http client"})
        self.assertNotIn("not allowed", out.lower())
        self.assertIn("docs.example", out)

    def test_websearch_uses_no_redirect_opener(self):
        # endpoint redirects must not be auto-followed; _NO_REDIRECT_OPENER.open is used
        s = _DummySettings()
        s.websearch_provider = "searxng"
        s.websearch_endpoint = "https://search.example/search"
        tools = self._tools(s, {"decision": "allow", "allow_websearch": True})
        calls = []
        def fake_open(req, timeout=None):  # noqa: ARG001
            calls.append(getattr(req, "full_url", req.get_full_url()))
            raise Exception("redirect response not expected to be auto-followed")
        with patch("app.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
             patch("app.tools.web._NO_REDIRECT_OPENER.open", side_effect=fake_open):
            out = tools["websearch"].invoke({"query": "python http client"})
        self.assertIn("ERROR", out)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
