"""Web access tools for research with consent, guardrails, and audit hooks.

Stdlib-only implementation. Network access is never performed unless policy and
explicit per-run consent allow it. Web/repo content is treated as untrusted data.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from langchain_core.tools import tool

_DEFAULT_WEBFETCH_TIMEOUT = 20
_DEFAULT_WEBFETCH_MAX_BYTES = 512000
_DEFAULT_WEBSEARCH_PROVIDER = "none"
_MAX_REDIRECTS = 5
_MAX_QUERY_LEN = 300
_MAX_URLS_ONLY = 50

_SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{8,}",
    r"AKIA[0-9A-Z]{16}",
    r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*\S+",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_CODEBLOCK_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_URL_RE = re.compile(r"\b(?:https?|file|ftp)://[^\s<>\"']+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>]+")
_UNIX_PATH_RE = re.compile(r"(?:^|[\s\"'`(])(/(?:home|etc|var|usr|proc|root|tmp|Users)[^\s\"'`)]*)")



class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None

    def _no_redirect(self, req, fp, code, msg, headers):  # noqa: ARG002
        return None

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _no_redirect


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)


class _HTMLToText(HTMLParser):
    _SKIP = {"script", "style", "noscript", "head", "meta", "link"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag.lower() in self._SKIP:
            self._skip_depth += 1
        if tag.lower() in {"br", "p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in {"p", "div", "li", "tr", "section", "article"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def get_text(self) -> str:
        text = html.unescape("".join(self._parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def _domain_matches(host: str, pattern: str) -> bool:
    host = _normalize_host(host)
    pat = _normalize_host(pattern)
    if not host or not pat:
        return False
    if pat.startswith("*."):
        suffix = pat[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix.lstrip(".")
    return host == pat


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_resolved_host(host: str, port: int | None) -> str | None:
    host_n = _normalize_host(host)
    if not host_n:
        return "missing host"
    if host_n in {"localhost", "localhost.localdomain"}:
        return "local/loopback hosts are not allowed"
    # Literal IP
    try:
        ip = ipaddress.ip_address(host_n)
        if _is_forbidden_ip(ip):
            return "private, loopback, link-local, or reserved IPs are not allowed"
        return None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host_n, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "could not resolve host"
    if not infos:
        return "could not resolve host"
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return "resolved to an invalid IP address"
        if _is_forbidden_ip(ip):
            return "host resolves to private, loopback, link-local, or reserved IP"
    return None


def _sanitize_url_for_audit(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = _normalize_host(parts.hostname)
        scheme = (parts.scheme or "").lower()
        if not host:
            return ""
        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"
        # drop credentials/query/fragment/path details
        return urlunsplit((scheme, netloc, "/", "", ""))
    except Exception:
        return ""


def _sanitize_text(value: str, max_len: int = 200) -> str:
    text = str(value or "")
    text = _CODEBLOCK_RE.sub("[code-omitted]", text)
    text = _EMAIL_RE.sub("[email-omitted]", text)
    text = _URL_RE.sub("[url-omitted]", text)
    text = _WINDOWS_PATH_RE.sub("[path-omitted]", text)
    text = _UNIX_PATH_RE.sub(lambda m: " [path-omitted]", text)
    for pat in _SECRET_PATTERNS:
        text = re.sub(pat, "[secret-omitted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def sanitize_query(query: str) -> tuple[str | None, str | None]:
    """Return (sanitized_query, rejection_reason)."""
    if query is None:
        return None, "query is required"
    raw = str(query)
    if len(raw) > _MAX_QUERY_LEN:
        return None, f"query too long (max {_MAX_QUERY_LEN} characters)"
    if _CODEBLOCK_RE.search(raw):
        return None, "query contains code blocks"
    if _EMAIL_RE.search(raw):
        return None, "query contains email addresses"
    if _URL_RE.search(raw):
        return None, "query contains URLs"
    if _WINDOWS_PATH_RE.search(raw) or _UNIX_PATH_RE.search(raw):
        return None, "query contains file paths"
    for pat in _SECRET_PATTERNS:
        if re.search(pat, raw):
            return None, "query appears to contain secrets"
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned:
        return None, "query is empty"
    if len(cleaned) < 3:
        return None, "query is too short"
    # Must look like a technical/generic query (not arbitrary project dump)
    if len(cleaned.split()) > 25:
        return None, "query is too specific/long; use a short generic technical query"
    return cleaned, None


def web_access_decision(settings: Any, consent: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve effective web access decision for a run.

    ``consent`` may contain: decision (ask|allow|deny|allow_urls), allowed_urls (list),
    allow_websearch (bool). ``--allow-web`` maps to allow, ``--no-web`` maps to deny.
    """
    consent = dict(consent or {})
    base = str(getattr(settings, "web_access", "ask") or "ask").strip().lower()
    if base not in {"ask", "allow", "deny"}:
        base = "ask"
    decision = str(consent.get("decision") or base).strip().lower()
    if base == "deny":
        decision = "deny"
    if base == "allow" and decision == "ask":
        decision = "allow"
    if decision not in {"ask", "allow", "deny", "allow_urls"}:
        decision = "deny"

    allowed_urls_raw = consent.get("allowed_urls") or []
    allowed_urls: list[str] = []
    if isinstance(allowed_urls_raw, list):
        for u in allowed_urls_raw[:_MAX_URLS_ONLY]:
            if isinstance(u, str) and u.strip():
                allowed_urls.append(u.strip())

    allow_websearch = bool(consent.get("allow_websearch", decision == "allow"))
    if decision in {"deny", "ask", "allow_urls"}:
        allow_websearch = False
    allow_webfetch = decision in {"allow", "allow_urls"}
    if decision == "ask":
        allow_webfetch = False
        allow_websearch = False

    provider = str(getattr(settings, "websearch_provider", _DEFAULT_WEBSEARCH_PROVIDER) or _DEFAULT_WEBSEARCH_PROVIDER).strip().lower()
    if provider not in {"none", "searxng", "tavily", "brave"}:
        provider = "none"
    endpoint = getattr(settings, "websearch_endpoint", None)
    endpoint_ok = bool(endpoint and str(endpoint).strip())
    provider_configured = provider != "none" and endpoint_ok

    return {
        "decision": decision,
        "allow_webfetch": allow_webfetch,
        "allow_websearch": allow_websearch and provider_configured,
        "websearch_configured": provider_configured,
        "websearch_provider": provider,
        "allowed_urls": allowed_urls,
        "urls_only": decision == "allow_urls",
    }


def write_web_audit(
    workspace_root: Path | str,
    event: dict[str, Any],
    consent: dict[str, Any] | None = None,
) -> str | None:
    """Append a sanitized audit record to ``.agents/research/audit.jsonl``.

    Never writes project content, secrets, raw queries/URLs, or credentials.
    """
    try:
        root = Path(workspace_root).expanduser().resolve()
        audit_dir = root / ".agents" / "research"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / "audit.jsonl"
        safe: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "event": str(event.get("event") or "web_access"),
            "decision": str((consent or {}).get("decision") or event.get("decision") or "unknown"),
        }
        for key in ("scope", "provider", "tool", "status", "reason", "http_status", "domain", "sanitized_query"):
            if key in event and event[key] is not None:
                val = event[key]
                if isinstance(val, str):
                    safe[key] = _sanitize_text(val, 120)
                elif isinstance(val, int):
                    safe[key] = val
                else:
                    safe[key] = _sanitize_text(str(val), 120)
        if "url" in event and isinstance(event.get("url"), str):
            safe["domain"] = _normalize_host(urlsplit(event["url"]).hostname) or safe.get("domain")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
        return str(path)
    except Exception:
        return None


def _ensure_fetch_allowed(
    url: str,
    settings: Any,
    consent: dict[str, Any] | None,
    *,
    bypass_domain_allowlist: bool = False,
) -> tuple[str | None, str | None, dict[str, Any]]:
    decision = web_access_decision(settings, consent)
    if not decision["allow_webfetch"]:
        return None, "web access denied or not consented for this run", decision
    try:
        parts = urlsplit(str(url).strip())
    except Exception:
        return None, "invalid URL", decision
    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None, "only http/https URLs are allowed", decision
    if parts.username or parts.password:
        return None, "URLs with embedded credentials are not allowed", decision
    host = _normalize_host(parts.hostname)
    if not host:
        return None, "URL host is missing", decision
    allowed_domains = list(getattr(settings, "webfetch_allowed_domains", []) or [])
    if allowed_domains and not bypass_domain_allowlist:
        if not any(_domain_matches(host, p) for p in allowed_domains):
            return None, "domain is not in WEBFETCH_ALLOWED_DOMAINS", decision
    if decision["urls_only"]:
        allowed_urls = decision.get("allowed_urls") or []
        norm = _normalize_host(parts.hostname)
        path = parts.path or "/"
        allowed_match = False
        for au in allowed_urls:
            try:
                ap = urlsplit(au)
                ah = _normalize_host(ap.hostname)
                ascheme = (ap.scheme or "").lower()
                apath = ap.path or "/"
                if ascheme in {"http", "https"} and ah == norm and (apath == "/" or path.startswith(apath.rstrip("/") or "/")):
                    allowed_match = True
                    break
            except Exception:
                continue
        if not allowed_match:
            return None, "URL is not in the user-provided allowlist for this run", decision
    err = _validate_resolved_host(host, parts.port)
    if err:
        return None, err, decision
    # normalize without credentials/fragment
    safe_url = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return safe_url, None, decision


def _read_limited(resp: Any, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = resp.read(min(65536, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            keep = max_bytes - sum(len(c) for c in chunks)
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


def _html_to_text(data: bytes, content_type: str) -> str:
    ct = (content_type or "").lower()
    charset = "utf-8"
    m = re.search(r"charset=([^\s;]+)", ct)
    if m:
        charset = m.group(1).strip("\"'") or "utf-8"
    try:
        text = data.decode(charset, errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")
    if "html" in ct or "<html" in text[:2000].lower() or "<body" in text[:2000].lower():
        parser = _HTMLToText()
        try:
            parser.feed(text)
            out = parser.get_text()
            if out:
                return out
        except Exception:
            pass
    return re.sub(r"\s+", " ", text).strip()


def _fetch_url(url: str, settings: Any, consent: dict[str, Any] | None) -> dict[str, Any]:
    timeout = int(getattr(settings, "webfetch_timeout", _DEFAULT_WEBFETCH_TIMEOUT) or _DEFAULT_WEBFETCH_TIMEOUT)
    max_bytes = int(getattr(settings, "webfetch_max_bytes", _DEFAULT_WEBFETCH_MAX_BYTES) or _DEFAULT_WEBFETCH_MAX_BYTES)
    current = url
    visited: set[str] = set()
    for _ in range(_MAX_REDIRECTS + 1):
        safe_url, err, decision = _ensure_fetch_allowed(current, settings, consent)
        if err:
            return {"ok": False, "error": err, "decision": decision, "url": _sanitize_url_for_audit(current)}
        assert safe_url is not None
        if safe_url in visited:
            return {"ok": False, "error": "redirect loop detected", "decision": decision, "url": _sanitize_url_for_audit(safe_url)}
        visited.add(safe_url)
        req = Request(safe_url, headers={"User-Agent": "opencode-research/1.0", "Accept": "text/*,application/json;q=0.9,*/*;q=0.1"})
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme/host validated; redirects disabled/revalidated manually
                status = getattr(resp, "status", 200)
                headers = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
                if 300 <= int(status) < 400:
                    loc = headers.get("location")
                    if not loc:
                        return {"ok": False, "error": "redirect without Location header", "decision": decision, "url": _sanitize_url_for_audit(safe_url)}
                    current = urljoin(safe_url, loc)
                    continue
                data, truncated = _read_limited(resp, max_bytes)
                content_type = headers.get("content-type", "")
                text = _html_to_text(data, content_type)
                if truncated:
                    text = (text + "\n\n[truncated]").strip()
                return {
                    "ok": True,
                    "url": _sanitize_url_for_audit(safe_url),
                    "status": int(status),
                    "content_type": content_type.split(";")[0].strip(),
                    "text": text,
                    "truncated": truncated,
                    "decision": decision,
                }
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            # Manual redirect handling with per-hop revalidation (auto-redirects disabled).
            if status and 300 <= int(status) < 400:
                loc = None
                try:
                    loc = exc.headers.get("Location") if exc.headers else None
                except Exception:
                    loc = None
                if loc:
                    current = urljoin(safe_url, loc)
                    continue
            return {"ok": False, "error": f"HTTP error: {status}", "decision": decision, "url": _sanitize_url_for_audit(safe_url)}
        except URLError:
            return {"ok": False, "error": "network error while fetching URL", "decision": decision, "url": _sanitize_url_for_audit(safe_url)}
        except Exception:
            return {"ok": False, "error": "failed to fetch URL", "decision": decision, "url": _sanitize_url_for_audit(safe_url)}
    return {"ok": False, "error": "too many redirects", "decision": web_access_decision(settings, consent), "url": _sanitize_url_for_audit(current)}


def _extract_search_items(payload: Any, provider: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if provider == "searxng" and isinstance(payload, dict):
        for r in (payload.get("results") or [])[:10]:
            if isinstance(r, dict):
                items.append({
                    "title": str(r.get("title") or "")[:200],
                    "url": str(r.get("url") or "")[:500],
                    "snippet": str(r.get("content") or r.get("snippet") or "")[:400],
                })
    elif provider == "tavily" and isinstance(payload, dict):
        for r in (payload.get("results") or [])[:10]:
            if isinstance(r, dict):
                items.append({
                    "title": str(r.get("title") or "")[:200],
                    "url": str(r.get("url") or "")[:500],
                    "snippet": str(r.get("content") or "")[:400],
                })
    elif provider == "brave" and isinstance(payload, dict):
        web = payload.get("web") if isinstance(payload.get("web"), dict) else {}
        for r in (web.get("results") or [])[:10]:
            if isinstance(r, dict):
                items.append({
                    "title": str(r.get("title") or "")[:200],
                    "url": str(r.get("url") or "")[:500],
                    "snippet": str(r.get("description") or "")[:400],
                })
    return [i for i in items if i.get("url") or i.get("title")]


def _search_web(query: str, settings: Any, consent: dict[str, Any] | None) -> dict[str, Any]:
    decision = web_access_decision(settings, consent)
    if not decision["allow_websearch"]:
        reason = "not configured"
        if not decision.get("websearch_configured"):
            reason = "web search is not configured"
        elif decision["decision"] in {"deny", "ask"}:
            reason = "web search denied or not consented for this run"
        elif decision.get("urls_only"):
            reason = "web search disabled (URLs-only consent)"
        else:
            reason = "web search denied"
        return {"ok": False, "error": reason, "decision": decision}

    sanitized, reject = sanitize_query(query)
    if reject or sanitized is None:
        return {"ok": False, "error": f"query rejected: {reject}", "decision": decision}

    provider = decision["websearch_provider"]
    endpoint = str(getattr(settings, "websearch_endpoint", "") or "").strip()
    # Validate endpoint like a fetch target (SSRF/scheme/IP checks intact). Exempt from
    # WEBFETCH_ALLOWED_DOMAINS so a fetch allowlist does not unintentionally block search.
    safe_endpoint, err, _ = _ensure_fetch_allowed(
        endpoint, settings, {"decision": "allow", "allow_websearch": False}, bypass_domain_allowlist=True
    )
    if err or not safe_endpoint:
        return {"ok": False, "error": "web search endpoint is not allowed", "decision": decision}

    timeout = int(getattr(settings, "webfetch_timeout", _DEFAULT_WEBFETCH_TIMEOUT) or _DEFAULT_WEBFETCH_TIMEOUT)
    max_bytes = int(getattr(settings, "webfetch_max_bytes", _DEFAULT_WEBFETCH_MAX_BYTES) or _DEFAULT_WEBFETCH_MAX_BYTES)
    headers = {"User-Agent": "opencode-research/1.0", "Accept": "application/json,text/plain,*/*"}
    api_key = getattr(settings, "websearch_api_key", None)
    body: bytes | None = None
    url = safe_endpoint

    try:
        if provider == "searxng":
            ep = urlsplit(safe_endpoint)
            params = dict(parse_qsl(ep.query, keep_blank_values=True))
            params["q"] = sanitized
            params["format"] = "json"
            url = urlunsplit((ep.scheme, ep.netloc, ep.path, urlencode(params), ""))
        elif provider == "tavily":
            body = json.dumps({"query": sanitized, "max_results": 5}).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif provider == "brave":
            ep = urlsplit(safe_endpoint)
            params = dict(parse_qsl(ep.query, keep_blank_values=True))
            params["q"] = sanitized
            url = urlunsplit((ep.scheme, ep.netloc, ep.path, urlencode(params), ""))
            if api_key:
                headers["X-Subscription-Token"] = str(api_key)
        req = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - endpoint validated, redirects disabled
            data, truncated = _read_limited(resp, max_bytes)
            ctype = (resp.headers.get("content-type", "") if resp.headers else "").lower()
            if "json" not in ctype:
                # best effort json parse anyway
                pass
            try:
                payload = json.loads(data.decode("utf-8", errors="replace") or "{}")
            except Exception:
                return {"ok": False, "error": "search provider returned invalid JSON", "decision": decision}
        items = _extract_search_items(payload, provider)
        return {
            "ok": True,
            "provider": provider,
            "query": sanitized,
            "results": items,
            "truncated": truncated,
            "decision": decision,
        }
    except Exception:
        return {"ok": False, "error": "web search request failed", "decision": decision}


def make_web_tools(
    workspace_root: Path | str,
    policy: Any,
    settings: Any,
    web_consent: dict[str, Any] | None = None,
) -> list[Any]:
    """Create consent-aware web tools for agents whose policy allows them."""
    root = Path(workspace_root).expanduser().resolve()
    consent = dict(web_consent or {})
    tools: list[Any] = []

    def _has_allow(action: str) -> bool:
        for rule in getattr(policy, "rules", []):
            if (str(rule.action).lower() == action.lower() or str(rule.action) == "*") and str(rule.effect).lower() == "allow":
                return True
        return False

    if _has_allow("webfetch"):
        @tool
        def webfetch(url: str) -> str:
            """Fetch an allowed http/https URL and return extracted text (consent-gated)."""
            try:
                result = _fetch_url(url, settings, consent)
                write_web_audit(
                    root,
                    {
                        "event": "webfetch",
                        "tool": "webfetch",
                        "url": str(url),
                        "status": "ok" if result.get("ok") else "denied_or_error",
                        "reason": result.get("error"),
                        "http_status": result.get("status"),
                    },
                    consent={**consent, "decision": (result.get("decision") or {}).get("decision", consent.get("decision", "ask"))},
                )
                if not result.get("ok"):
                    return f"ERROR: {result.get('error', 'webfetch denied')}"
                text = str(result.get("text") or "")
                if not text:
                    text = "(no text content)"
                return (
                    f"URL: {result.get('url', '')}\n"
                    f"Status: {result.get('status')}\n"
                    f"Content-Type: {result.get('content_type', '')}\n\n{text}"
                )
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.append(webfetch)

    if _has_allow("websearch"):
        @tool
        def websearch(query: str) -> str:
            """Search the web via configured provider with sanitized query (consent-gated)."""
            try:
                result = _search_web(query, settings, consent)
                sanitized_q = result.get("query") or _sanitize_text(query, 120)
                write_web_audit(
                    root,
                    {
                        "event": "websearch",
                        "tool": "websearch",
                        "provider": result.get("provider") or getattr(settings, "websearch_provider", "none"),
                        "sanitized_query": sanitized_q,
                        "status": "ok" if result.get("ok") else "denied_or_error",
                        "reason": result.get("error"),
                    },
                    consent={**consent, "decision": (result.get("decision") or {}).get("decision", consent.get("decision", "ask"))},
                )
                if not result.get("ok"):
                    return f"ERROR: {result.get('error', 'websearch denied')}"
                lines = [f"Provider: {result.get('provider')}", f"Query: {result.get('query', '')}", "Results:"]
                for i, item in enumerate(result.get("results") or [], 1):
                    title = str(item.get("title") or "").strip()
                    url = str(item.get("url") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    # do not echo raw secrets/paths; keep as provided by provider but bounded
                    lines.append(f"{i}. {title}\n   {url}\n   {snippet}".strip())
                if not (result.get("results") or []):
                    lines.append("(no results)")
                return "\n".join(lines)
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.append(websearch)

    return tools


__all__ = [
    "make_web_tools",
    "sanitize_query",
    "web_access_decision",
    "write_web_audit",
]