"""Tests for the local-search MCP websearch server.

Covers:
  - SSRF guard (_validate_public_http_url): rejects loopback, RFC1918,
    link-local, IPv6 local, localhost domains, redirect-to-private; allows
    public hosts.
  - web_fetch tool error contract for rejected URLs.
  - web_search error payload shape when Brave fails.
  - web_search success payload shape (mocked Brave) matches the contract
    that Home Automation's McpSearchProvider expects (result.content[].text
    is JSON with results/suggestions/text).

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

import pytest

import server as srv


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool through FastMCP's in-memory client and return the raw result."""
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        return await client.call_tool(tool_name, arguments)


async def _allow_public_url(_url: str):
    return [srv.ipaddress.ip_address("8.8.8.8")]


def _result_text(result: Any) -> str:
    """Mirror Home Automation's _mcp_result_text extraction.

    Handles both raw dicts (result.content[]) and FastMCP CallToolResult,
    whose .content items expose .text (and may also be dict-like).
    """
    content = None
    if isinstance(result, dict):
        content = result.get("content")
    else:
        content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return json.dumps(result, default=str)


def _structured_content(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        value = result.get("structuredContent") or result.get("structured_content")
    else:
        value = getattr(result, "structured_content", None)
    return value if isinstance(value, dict) else {}


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch, tmp_path):
    cache = srv.WebCache(tmp_path / "cache")
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_cache", cache)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    yield
    cache.close()
    telemetry.close()


def _brave_outcome(results=None, *, state="ok", **extra) -> srv._BackendOutcome:
    """Build a Brave _BackendOutcome with normalized results."""
    if results is None:
        results = []
    return srv._BackendOutcome(
        backend="brave",
        ok=state in {"ok", "empty"},
        state=state,
        results=results,
        attempts=extra.pop("attempts", 1),
        **extra,
    )




def test_http_transport_does_not_info_log_search_urls():
    import http_server  # noqa: F401
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


# --------------------------------------------------------------------------- #
# SSRF guard.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reject_loopback_ipv4():
    with pytest.raises(ValueError, match="local/private"):
        await srv._validate_public_http_url("http://127.0.0.1/")

@pytest.mark.asyncio
async def test_reject_loopback_ipv6():
    with pytest.raises(ValueError, match="local/private"):
        await srv._validate_public_http_url("http://[::1]/")

@pytest.mark.asyncio
async def test_reject_rfc1918():
    for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        with pytest.raises(ValueError, match="local/private"):
            await srv._validate_public_http_url(f"http://{ip}/")

@pytest.mark.asyncio
async def test_reject_link_local():
    with pytest.raises(ValueError, match="local/private"):
        await srv._validate_public_http_url("http://169.254.169.254/latest/meta-data/")

@pytest.mark.asyncio
async def test_reject_localhost_domain():
    for h in ("localhost", "local", "foo.localhost"):
        with pytest.raises(ValueError, match="local/private|local/private URL"):
            await srv._validate_public_http_url(f"http://{h}/")

@pytest.mark.asyncio
async def test_reject_non_http_scheme():
    with pytest.raises(ValueError, match="public http"):
        await srv._validate_public_http_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="public http"):
        await srv._validate_public_http_url("gopher://example.com/")

@pytest.mark.asyncio
async def test_reject_domain_resolving_private(monkeypatch):
    """A public-looking hostname that resolves to a private IP must be rejected."""
    async def fake_resolve(host, port, scheme):
        import ipaddress
        return [ipaddress.ip_address("10.1.2.3")]
    monkeypatch.setattr(srv, "_resolve_host_ips", fake_resolve)
    with pytest.raises(ValueError, match="local/private"):
        await srv._validate_public_http_url("http://internal.lookup.example.com/")

@pytest.mark.asyncio
async def test_allow_public_ip(monkeypatch):
    """A public IP must pass (no resolution needed)."""
    await srv._validate_public_http_url("http://8.8.8.8/")  # should not raise

@pytest.mark.asyncio
async def test_allow_public_domain(monkeypatch):
    import ipaddress
    async def fake_resolve(host, port, scheme):
        return [ipaddress.ip_address("93.184.216.34")]  # example.com
    monkeypatch.setattr(srv, "_resolve_host_ips", fake_resolve)
    addresses = await srv._validate_public_http_url("https://example.com/")
    assert addresses == [ipaddress.ip_address("93.184.216.34")]


@pytest.mark.asyncio
async def test_reject_credential_bearing_url():
    with pytest.raises(ValueError, match="Credential-bearing"):
        await srv._validate_public_http_url("https://user:pass@example.com/")


def test_pinned_public_request_preserves_host_and_tls_sni():
    address = srv.ipaddress.ip_address("93.184.216.34")
    url, host, extensions = srv._pinned_public_request(
        "https://example.com:8443/path?q=1#fragment",
        address,
    )
    assert url == "https://93.184.216.34:8443/path?q=1"
    assert host == "example.com:8443"
    assert extensions == {"sni_hostname": "example.com"}


# --------------------------------------------------------------------------- #
# web_fetch tool contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_web_fetch_rejects_loopback_with_error_prefix():
    result = await _call_tool("web_fetch", {"url": "http://127.0.0.1:8888/healthz"})
    text = _result_text(result)
    assert text.startswith("Fetch error:")
    assert "local/private" in text or "Refusing" in text

@pytest.mark.asyncio
async def test_web_fetch_rejects_metadata_ip():
    result = await _call_tool("web_fetch", {"url": "http://169.254.169.254/"})
    text = _result_text(result)
    assert text.startswith("Fetch error:")


def test_html_extractor_preserves_safe_attachment_links_and_base_href():
    parser = srv._TextExtractor("https://example.com/code/")
    parser.feed(
        '<base href="https://cdn.example.com/current/">'
        '<p>Schedule <a href="rules.pdf"><span>Attachment 1</span></a></p>'
        '<a href="javascript:alert(1)">unsafe</a>'
        '<script><a href="/bad.pdf">ignore me</a></script>'
    )
    text = parser.get_text()
    assert "Schedule" in text
    assert "[Attachment 1](https://cdn.example.com/current/rules.pdf)" in text
    assert "javascript:" not in text
    assert "bad.pdf" not in text


def test_html_main_content_removes_boilerplate_and_preserves_article_links():
    article = (
        "The zoning ordinance explains setback requirements, permitted uses, "
        "application deadlines, and appeal procedures for property owners. " * 8
    )
    html = (
        "<html><body><nav>HOME NAVIGATION PRICING LOGIN</nav>"
        f'<main><article><h1>Planning Guide</h1><p>{article}</p>'
        '<a href="details.html">Read details</a></article></main>'
        "<footer>COOKIE POLICY PRIVACY TERMS SOCIAL</footer></body></html>"
    )
    text = srv._extract_html_content(html, "https://example.com/guides/page")
    assert "Planning Guide" in text
    assert "setback requirements" in text
    assert "[Read details](https://example.com/guides/details.html)" in text
    assert "HOME NAVIGATION" not in text
    assert "COOKIE POLICY" not in text


def test_html_main_content_retains_attachments_omitted_from_article():
    article = "Primary article content with enough detail for extraction. " * 12
    html = (
        '<base href="https://cdn.example.com/files/">'
        f"<article><p>{article}</p></article>"
        '<footer><a href="schedule.pdf">Official schedule</a>'
        '<a href="javascript:alert(1)" download>Unsafe</a></footer>'
    )
    text = srv._extract_html_content(html, "https://example.com/page")
    assert "Primary article content" in text
    assert "Attachments:" in text
    assert "[Official schedule](https://cdn.example.com/files/schedule.pdf)" in text
    assert "javascript:" not in text


def test_html_main_content_keeps_late_in_article_attachment_after_truncation():
    html = (
        "<article><p>" + ("Long article content. " * 200) + "</p>"
        '<a href="late-report.pdf">Late report</a></article>'
    )
    text = srv._extract_html_content(html, "https://example.com/page", 200)
    assert len(text) <= 200
    assert "[Late report](https://example.com/late-report.pdf)" in text


def test_html_main_content_bounds_attachment_labels():
    html = (
        "<article><p>" + ("Useful article content. " * 30) + "</p></article>"
        f'<footer><a href="report.pdf">{"label " * 500}</a></footer>'
    )
    text = srv._extract_html_content(html, "https://example.com/page", 500)
    assert len(text) <= 500
    assert "https://example.com/report.pdf" in text
    assert ("label " * 50) not in text


def test_html_main_content_resolves_relative_base_once():
    article = "Detailed article content for a relative base URL regression. " * 12
    html = (
        '<base href="assets/">'
        f'<article><p>{article}</p><a href="details.html">Details</a></article>'
    )
    text = srv._extract_html_content(html, "https://example.com/path/page")
    assert "[Details](https://example.com/path/assets/details.html)" in text
    assert "assets/assets" not in text


def test_html_main_content_falls_back_on_short_or_failed_readability(monkeypatch):
    class BrokenDocument:
        def __init__(self, *args, **kwargs):
            raise ValueError("malformed")

    monkeypatch.setattr(srv.htmlx, "Document", BrokenDocument)
    html = '<nav>Navigation</nav><p>Short but useful fact.</p><a href="report.pdf">Report</a>'
    text = srv._extract_html_content(html, "https://example.com/page")
    assert "Navigation" in text
    assert "Short but useful fact." in text
    assert text.count("https://example.com/report.pdf") == 1


def test_html_main_content_skips_readability_over_size_limit(monkeypatch):
    class UnexpectedDocument:
        def __init__(self, *args, **kwargs):
            raise AssertionError("readability should be skipped")

    monkeypatch.setattr(srv.htmlx, "Document", UnexpectedDocument)
    monkeypatch.setattr(srv.htmlx, "MAIN_CONTENT_MAX_CHARS", 10)
    text = srv._extract_html_content("<p>Useful oversized document text.</p>", "https://example.com/")
    assert text == "Useful oversized document text."


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class _StaticResponse:
    is_redirect = False
    status_code = 200

    def __init__(self, url: str, body: bytes, content_type: str):
        self.url = srv.httpx.URL(url)
        self.body = body
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None

    async def aiter_raw(self):
        yield self.body


@pytest.mark.asyncio
async def test_fetch_pins_validated_ip_against_dns_rebinding(monkeypatch):
    resolutions = 0
    request: dict[str, Any] = {}

    async def alternating_resolve(host, port, scheme):
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [srv.ipaddress.ip_address(address)]

    class FakeClient:
        def stream(self, method, url, **kwargs):
            request.update({"url": url, **kwargs})
            return _FakeStream(_StaticResponse(url, b"safe", "text/plain"))

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_resolve_host_ips", alternating_resolve)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    final_url, body, content_type, status = await srv._fetch_public_body(
        "https://example.com/path"
    )

    assert resolutions == 1
    assert request["url"] == "https://93.184.216.34/path"
    assert request["headers"]["Host"] == "example.com"
    assert request["headers"]["User-Agent"].endswith("Chrome/131.0.0.0 Safari/537.36")
    assert request["headers"]["Accept-Encoding"] == srv._FETCH_ACCEPT_ENCODING
    assert request["headers"]["Accept-Language"] == "en-US,en;q=0.9"
    assert request["headers"]["Sec-Fetch-Dest"] == "document"
    assert request["headers"]["Sec-Fetch-Mode"] == "navigate"
    assert request["headers"]["Sec-Fetch-Site"] == "none"
    assert request["headers"]["Sec-Fetch-User"] == "?1"
    assert request["headers"]["Upgrade-Insecure-Requests"] == "1"
    assert request["extensions"] == {"sni_hostname": "example.com"}
    assert final_url == "https://example.com/path"
    assert body == b"safe"
    assert content_type == "text/plain"
    assert status == 200


@pytest.mark.asyncio
async def test_fetch_revalidates_and_rejects_private_redirect(monkeypatch):
    calls = 0

    class RedirectResponse:
        is_redirect = True
        headers = {"location": "http://127.0.0.1/admin"}
        url = srv.httpx.URL("https://93.184.216.34/start")

    class FakeClient:
        def stream(self, method, url, **kwargs):
            nonlocal calls
            calls += 1
            return _FakeStream(RedirectResponse())

    async def fake_client():
        return FakeClient()

    async def public_example(host, port, scheme):
        try:
            return [srv.ipaddress.ip_address(host)]
        except ValueError:
            return [srv.ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(srv, "_resolve_host_ips", public_example)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    with pytest.raises(ValueError, match="local/private"):
        await srv._fetch_public_body("https://example.com/start")
    assert calls == 1


@pytest.mark.asyncio
async def test_fetch_uses_fresh_tls_pool_for_each_redirect_host(monkeypatch):
    created = 0
    closed = 0
    requests: list[tuple[str, str, dict[str, Any]]] = []

    class RedirectResponse:
        is_redirect = True
        headers = {"location": "https://second.example/final"}
        url = srv.httpx.URL("https://93.184.216.34/start")

    class HopClient:
        def __init__(self, hop: int):
            self.hop = hop

        def stream(self, method, url, **kwargs):
            requests.append((url, kwargs["headers"]["Host"], kwargs["extensions"]))
            response = (
                RedirectResponse()
                if self.hop == 1
                else _StaticResponse(url, b"safe", "text/plain")
            )
            return _FakeStream(response)

        async def aclose(self):
            nonlocal closed
            closed += 1

    async def fresh_client():
        nonlocal created
        created += 1
        return HopClient(created)

    async def same_public_ip(host, port, scheme):
        return [srv.ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(srv, "_resolve_host_ips", same_public_ip)
    monkeypatch.setattr(srv, "_public_fetch_client", fresh_client)
    final_url, body, _, status = await srv._fetch_public_body(
        "https://first.example/start"
    )

    assert created == closed == 2
    assert requests == [
        (
            "https://93.184.216.34/start",
            "first.example",
            {"sni_hostname": "first.example"},
        ),
        (
            "https://93.184.216.34/final",
            "second.example",
            {"sni_hostname": "second.example"},
        ),
    ]
    assert final_url == "https://second.example/final"
    assert body == b"safe"
    assert status == 200


@pytest.mark.asyncio
async def test_fetch_rejects_encoded_response_before_reading(monkeypatch):
    streamed = False
    request_headers: dict[str, str] = {}

    class EncodedResponse:
        is_redirect = False
        status_code = 200
        headers = {
            "content-type": "text/plain",
            "content-encoding": "gzip",
        }

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            nonlocal streamed
            streamed = True
            yield b"encoded bytes must not be consumed"

    class FakeClient:
        def stream(self, method, url, **kwargs):
            request_headers.update(kwargs["headers"])
            return _FakeStream(EncodedResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    with pytest.raises(ValueError, match="unsupported content encoding: gzip"):
        await srv._fetch_public_body("https://example.com/compressed")
    assert request_headers["Accept-Encoding"] == "identity"
    assert streamed is False


@pytest.mark.asyncio
async def test_web_fetch_records_host_only_success_telemetry(monkeypatch):
    async def successful_fetch(url):
        return url, b"fetched text", "text/plain", 200

    monkeypatch.setattr(srv, "_fetch_public_body", successful_fetch)
    result = await _call_tool(
        "web_fetch",
        {"url": "https://Example.COM/private/path?q=secret"},
    )
    assert _result_text(result) == "fetched text"
    assert srv._telemetry is not None
    assert srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT url_host, http_status, outcome, tier_used, bytes "
            "FROM fetch_events"
        ).fetchone()
    assert row == ("example.com", 200, "success", "direct", 12)
    assert "private" not in repr(row)
    assert "secret" not in repr(row)


@pytest.mark.asyncio
async def test_web_fetch_cache_hit_skips_network_and_exposes_metadata(monkeypatch):
    calls = 0

    async def successful_fetch(url):
        nonlocal calls
        calls += 1
        return url, b"cached fetched text", "text/plain", 200

    monkeypatch.setattr(srv, "_fetch_public_body", successful_fetch)
    first = await _call_tool("web_fetch", {"url": "https://example.com/cached"})
    second = await _call_tool("web_fetch", {"url": "https://example.com/cached"})
    assert _result_text(first) == "cached fetched text"
    assert _result_text(second) == "cached fetched text"
    assert _structured_content(first)["cache_hit"] is False
    assert _structured_content(second)["cache_hit"] is True
    assert calls == 1

    assert srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        cache_outcomes = conn.execute(
            "SELECT cache_hit FROM fetch_events ORDER BY id"
        ).fetchall()
    assert cache_outcomes == [(0,), (1,)]


@pytest.mark.asyncio
async def test_web_fetch_records_http_error_telemetry(monkeypatch):
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", False)

    async def failed_fetch(_url):
        request = srv.httpx.Request("GET", "https://example.com/")
        response = srv.httpx.Response(403, request=request)
        raise srv.httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(srv, "_fetch_public_body", failed_fetch)
    result = await _call_tool(
        "web_fetch",
        {"url": "https://example.com/private?q=secret"},
    )
    assert _result_text(result).startswith("Fetch error: HTTP 403")
    assert srv._telemetry is not None
    assert srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT url_host, http_status, outcome, tier_used, bytes "
            "FROM fetch_events"
        ).fetchone()
    assert row == ("example.com", 403, "http_error", "direct", None)


@pytest.mark.asyncio
async def test_web_fetch_html_reserves_space_for_attachments(monkeypatch):
    article = "Long main article content that must be truncated safely. " * 100
    html = (
        f"<article><p>{article}</p></article>"
        '<footer><a href="report.pdf">Official report</a></footer>'
    ).encode()

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, html, "text/html; charset=utf-8"))

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    result = await _call_tool(
        "web_fetch",
        {"url": "https://example.com/page", "max_chars": 200},
    )
    text = _result_text(result)
    assert len(text) <= 200
    assert "Attachments:" in text
    assert "[Official report](https://example.com/report.pdf)" in text


@pytest.mark.asyncio
async def test_html_extraction_timeout_kills_isolated_child(monkeypatch, tmp_path):
    script = tmp_path / "slow_extractor.py"
    script.write_text("import sys,time\nsys.stdin.buffer.read()\ntime.sleep(10)\n")
    monkeypatch.setattr(srv, "HTML_EXTRACT_SCRIPT", script)
    monkeypatch.setattr(srv, "HTML_EXTRACT_TIMEOUT", 0.05)
    started = srv.time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await srv._extract_html_content_isolated(
            "<p>content</p>",
            "https://example.com/",
            1000,
        )
    assert srv.time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_extraction_admission_rejects_excess_without_queueing():
    admission: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    semaphore = asyncio.Semaphore(1)
    started = srv.time.monotonic()
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        async with srv._bounded_extraction_slot(admission, semaphore, 10, "HTML"):
            raise AssertionError("slot must not be admitted")
    assert srv.time.monotonic() - started < 0.1


@pytest.mark.asyncio
async def test_extraction_queue_wait_counts_against_deadline():
    admission: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    admission.put_nowait(None)
    semaphore = asyncio.Semaphore(0)
    started = srv.time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        async with srv._bounded_extraction_slot(admission, semaphore, 0.05, "HTML"):
            raise AssertionError("slot must time out")
    assert srv.time.monotonic() - started < 0.5
    assert admission.qsize() == 1


@pytest.mark.asyncio
async def test_web_fetch_pdf_returns_plain_extracted_text(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"%PDF-1.7 fake fixture", "application/pdf"))

    async def fake_client():
        return FakeClient()

    async def fake_extract(body, deadline=None):
        return "AC district Lot Size: 10 acres", "macos_vision_ocr"

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    monkeypatch.setattr(srv, "_extract_pdf_text", fake_extract)
    result = await _call_tool(
        "web_fetch",
        {"url": "https://example.com/Schedule.pdf", "max_chars": 5000},
    )
    text = _result_text(result)
    assert text.startswith("PDF: Schedule.pdf\nSource: https://example.com/Schedule.pdf")
    assert "Extraction: macos_vision_ocr" in text
    assert "AC district Lot Size: 10 acres" in text


@pytest.mark.asyncio
async def test_web_fetch_pdf_output_limit_uses_error_contract(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"%PDF-1.7 fake fixture", "application/pdf"))

    async def fake_client():
        return FakeClient()

    async def limited_extract(body, deadline=None):
        raise srv._OutputLimitExceeded("PDF command output exceeds limit")

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    monkeypatch.setattr(srv, "_extract_pdf_text", limited_extract)
    result = await _call_tool("web_fetch", {"url": "https://example.com/Schedule.pdf"})
    assert _result_text(result).startswith("Fetch error: PDF extraction failed: PDF command output exceeds limit")


def test_truncation_helpers_never_exceed_requested_length():
    assert len(srv._truncate_text("x" * 2000, 1000, "\n... (truncated)")) == 1000
    assert len(srv._pdf_fetch_text("https://example.com/a.pdf", "x" * 2000, "ocr", 1000)) == 1000


@pytest.mark.asyncio
async def test_web_fetch_never_decodes_unreadable_pdf_as_binary_text(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"%PDF-1.4\x00binary stream", "application/octet-stream"))

    async def fake_client():
        return FakeClient()

    async def fake_extract(body, deadline=None):
        return None, "PDF OCR produced no meaningful text"

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    monkeypatch.setattr(srv, "_extract_pdf_text", fake_extract)
    result = await _call_tool("web_fetch", {"url": "https://example.com/scan.pdf"})
    text = _result_text(result)
    assert text.startswith("Fetch error: PDF OCR produced no meaningful text")
    assert "%PDF" not in text


@pytest.mark.asyncio
async def test_pdf_command_output_limit_terminates_process():
    with pytest.raises(srv._OutputLimitExceeded):
        await srv._run_pdf_command(
            [srv.sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"],
            srv.time.monotonic() + 5,
            stdout_limit=100,
        )


@pytest.mark.asyncio
async def test_short_pdftotext_output_is_preserved_when_ocr_unavailable(monkeypatch):
    async def fake_command(args, deadline, **kwargs):
        return 0, b"Lot Size: 10 acres", b""

    monkeypatch.setattr(srv, "PDFTOTEXT", "/fake/pdftotext")
    monkeypatch.setattr(srv, "PDFTOPPM", None)
    monkeypatch.setattr(srv, "_run_pdf_command", fake_command)
    text, method = await srv._extract_pdf_text(b"%PDF-1.4 fixture")
    assert text == "Lot Size: 10 acres"
    assert method == "pdftotext_partial"


@pytest.mark.asyncio
async def test_web_fetch_caps_raw_chunks_and_stops_after_overflow(monkeypatch):
    chunks_consumed = 0

    class ChunkedResponse:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "text/plain"}
        url = srv.httpx.URL("https://example.com/large")

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            nonlocal chunks_consumed
            for chunk in (b"a" * 6, b"b" * 6, b"must not be consumed"):
                chunks_consumed += 1
                yield chunk

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(ChunkedResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "FETCH_MAX_BYTES", 10)
    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    result = await _call_tool("web_fetch", {"url": "https://example.com/large"})
    assert _result_text(result) == "Fetch error: response exceeds 10 byte limit"
    assert chunks_consumed == 2


@pytest.mark.asyncio
async def test_fetch_accepts_raw_body_at_exact_byte_limit(monkeypatch):
    class ExactResponse:
        is_redirect = False
        status_code = 200
        headers = {
            "content-type": "text/plain",
            "content-length": "10",
            "content-encoding": "identity",
        }

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            yield b"a" * 4
            yield b"b" * 6

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(ExactResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "FETCH_MAX_BYTES", 10)
    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    _, body, _, status = await srv._fetch_public_body("https://example.com/exact")
    assert body == b"a" * 4 + b"b" * 6
    assert status == 200


@pytest.mark.asyncio
async def test_fetch_rejects_oversized_content_length_before_stream(monkeypatch):
    streamed = False

    class OversizedResponse:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "text/plain", "content-length": "11"}

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            nonlocal streamed
            streamed = True
            yield b"not consumed"

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(OversizedResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "FETCH_MAX_BYTES", 10)
    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    with pytest.raises(ValueError, match="response exceeds 10 byte limit"):
        await srv._fetch_public_body("https://example.com/large")
    assert streamed is False


@pytest.mark.asyncio
async def test_fetch_http_status_precedes_oversized_content_length(monkeypatch):
    request = srv.httpx.Request("GET", "https://example.com/failure")
    response = srv.httpx.Response(413, request=request)

    class FailedResponse:
        is_redirect = False
        status_code = 413
        headers = {"content-type": "text/plain", "content-length": "999"}

        def raise_for_status(self):
            raise srv.httpx.HTTPStatusError(
                "too large", request=request, response=response
            )

        async def aiter_raw(self):
            raise AssertionError("error response body must not be consumed")
            yield b""  # pragma: no cover

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(FailedResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "FETCH_MAX_BYTES", 10)
    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    with pytest.raises(srv.httpx.HTTPStatusError):
        await srv._fetch_public_body("https://example.com/failure")


@pytest.mark.asyncio
async def test_web_fetch_honors_small_max_chars(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"abcdefghijklmnopqrstuvwxyz", "text/plain"))

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    result = await _call_tool("web_fetch", {"url": "https://example.com/text", "max_chars": 10})
    assert len(_result_text(result)) == 10


@pytest.mark.asyncio
async def test_web_fetch_rejects_binary_mislabeled_as_text(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"PNG\x00\x01\x02binary", "text/plain"))

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_public_fetch_client", fake_client)
    result = await _call_tool("web_fetch", {"url": "https://example.com/fake.txt"})
    assert _result_text(result) == "Fetch error: unsupported binary content type: text/plain"


# --------------------------------------------------------------------------- #
# web_search payload contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_web_search_error_payload_when_brave_fails(monkeypatch):
    """When Brave fails (no fallback configured), the tool returns a structured
    error payload (not an exception) with the keys consumers expect."""
    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="brave", state="error", error="boom")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "test", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["query"] == "test"
    assert payload["results"] == []
    assert payload["suggestions"] == []
    assert payload["error"]
    assert "text" in payload

@pytest.mark.asyncio
async def test_web_search_rejects_oversized_query_before_provider_access(monkeypatch):
    monkeypatch.setattr(
        srv,
        "_brave_search",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )
    secret_suffix = "PRIVATE_SUFFIX"
    result = await _call_tool(
        "web_search",
        {"query": "x" * srv.MAX_QUERY_CHARS + secret_suffix, "num_results": 3},
    )
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert str(srv.MAX_QUERY_CHARS) in payload["error"]
    assert secret_suffix not in _result_text(result)




@pytest.mark.asyncio
async def test_brave_base_url_requires_credential_free_https(monkeypatch):
    assert srv._brave_search_url() == "https://api.search.brave.com/res/v1/web/search"
    for invalid in (
        "http://api.search.brave.com",
        "https://user:pass@api.search.brave.com",
        "https://api.search.brave.com/v1",
        "https://api.search.brave.com:bad",
        "https://api.search.brave.com?token=secret",
    ):
        monkeypatch.setattr(srv, "BRAVE_BASE_URL", invalid)
        with pytest.raises(ValueError, match="credential-free HTTPS"):
            srv._brave_search_url()


@pytest.mark.asyncio
async def test_provider_json_response_is_size_bounded(monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_RESPONSE_MAX_BYTES", 8)
    response = srv.httpx.Response(200, stream=srv.httpx.ByteStream(b'{"result": true}'))
    with pytest.raises(ValueError, match="byte limit"):
        await srv._limited_json_object(response, provider="test")


@pytest.mark.asyncio
async def test_provider_json_response_rejects_compression_before_decode():
    response = srv.httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=srv.httpx.ByteStream(b"compressed bytes are never decoded"),
    )
    with pytest.raises(ValueError, match="content encoding"):
        await srv._limited_json_object(response, provider="test")




@pytest.mark.asyncio
async def test_web_search_filters_unsafe_urls_and_errors_when_none_are_usable(monkeypatch):
    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": "relative", "url": "/private", "snippet": "bad",
             "domain": None, "engine": "brave", "provider": "brave", "score": None},
            {"title": "script", "url": "javascript:alert(1)", "snippet": "bad",
             "domain": None, "engine": "brave", "provider": "brave", "score": None},
            {"title": "local", "url": "http://127.0.0.1/secret", "snippet": "bad",
             "domain": "127.0.0.1", "engine": "brave", "provider": "brave", "score": None},
            {"title": "short local", "url": "http://127.1/secret", "snippet": "bad",
             "domain": "127.1", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "safe sources", "num_results": 5})
    payload = json.loads(_result_text(result))
    assert payload["attempted"] == ["brave"]
    # All results filtered as unsafe → empty results.
    assert [item["url"] for item in payload["results"]] == []




@pytest.mark.asyncio
async def test_web_search_success_payload_shape(monkeypatch):
    """Success payload matches the contract Home Automation's McpSearchProvider
    relies on: result.content[].text is JSON with results/suggestions/text,
    and each result has rank/title/url/domain/snippet/engine."""
    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": "Example", "url": "https://example.com/a", "snippet": "snippet a",
             "domain": "example.com", "engine": "brave", "provider": "brave", "score": None},
            {"title": "Other", "url": "https://other.com/b", "snippet": "snippet b",
             "domain": "other.com", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "hello", "num_results": 8})
    payload = json.loads(_result_text(result))
    assert payload["query"] == "hello"
    assert len(payload["results"]) == 2
    first = payload["results"][0]
    for key in ("rank", "title", "url", "domain", "snippet", "engine"):
        assert key in first
    assert first["domain"] == "example.com"
    assert payload["suggestions"] == []
    assert "## Search: hello" in payload["text"]


@pytest.mark.asyncio
async def test_web_search_cache_hit_skips_provider_and_has_zero_cost(monkeypatch):
    calls = 0

    async def fake_brave_search(query, num_results, api_key):
        nonlocal calls
        calls += 1
        return _brave_outcome([
            {"title": "Cached", "url": "https://cache.example/a", "snippet": "s",
             "domain": "cache.example", "engine": "brave", "provider": "brave",
             "score": None},
        ])

    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    first = json.loads(_result_text(await _call_tool(
        "web_search", {"query": "cache integration", "num_results": 3}
    )))
    second = json.loads(_result_text(await _call_tool(
        "web_search", {"query": "cache integration", "num_results": 3}
    )))

    assert first["cache_hit"] is False
    assert first["cache_age_seconds"] == 0
    assert second["cache_hit"] is True
    assert second["attempted"] == []
    assert second["estimated_cost_usd"] == 0.0
    assert second["timings_ms"]["brave"] is None
    assert calls == 1

    assert srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        search_rows = conn.execute(
            "SELECT cache_hit FROM search_events ORDER BY id"
        ).fetchall()
        provider_count = conn.execute("SELECT COUNT(*) FROM provider_events").fetchone()[0]
    assert search_rows == [(0,), (1,)]
    assert provider_count == 1


@pytest.mark.asyncio
async def test_web_search_empty_results_returns_empty(monkeypatch):
    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([], state="empty")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "x"})
    payload = json.loads(_result_text(result))
    assert payload["results"] == []
    assert "No results found" in payload["text"]


# --------------------------------------------------------------------------- #
# Policy, deadlines, metadata, and health.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_success_payload_includes_provider_metadata(monkeypatch):
    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": "B", "url": "https://b.example/a", "snippet": "s",
             "domain": "b.example", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "ok"
    assert payload["backend"] == "brave"
    assert payload["attempted"] == ["brave"]
    assert payload["fallback_reason"] is None
    assert set(payload["timings_ms"]) == {"total", "brave"}
    assert payload["provider_states"] == {"brave": "ok"}
    assert "query" not in srv._last_search
    assert srv._last_search["result_count"] == 1


@pytest.mark.asyncio
async def test_degraded_is_distinct_from_empty_and_error(monkeypatch):
    """Brave error state surfaces as error (no fallback provider exists)."""
    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="brave", state="error", error="boom")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "error" in payload
    assert payload["provider_states"] == {"brave": "error"}
















@pytest.mark.asyncio
async def test_circuit_breaker_admits_one_half_open_probe(monkeypatch):
    monkeypatch.setattr(srv, "BREAKER_COOLDOWN", 10.0)
    monkeypatch.setattr(srv.time, "monotonic", lambda: 20.0)
    breaker = srv._CircuitBreaker()
    breaker._tripped["brave"] = 0.0
    breaker._fails["brave"] = 3
    assert breaker.allow("brave") is True
    assert breaker.allow("brave") is False
    assert breaker.snapshot("brave")["state"] == "half_open"


def test_circuit_breaker_reports_one_atomic_open_transition(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 2)
    breaker = srv._CircuitBreaker()
    with ThreadPoolExecutor(max_workers=8) as executor:
        transitions = list(executor.map(lambda _: breaker.record_failure("brave"), range(20)))
    assert transitions.count("opened") == 1
    assert transitions.count("reopened") == 0
    assert breaker.snapshot("brave")["state"] == "open"


# --------------------------------------------------------------------------- #
# Query-free telemetry and aggregate endpoint.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_search_telemetry_never_persists_query_or_result_content(monkeypatch):
    import sqlite3

    secret_query = "SECRET_QUERY_4cb09b"
    secret_result = "SECRET_RESULT_78a2f1"

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": secret_result, "url": f"https://example.com/{secret_result}",
             "snippet": secret_result, "domain": "example.com",
             "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": secret_query, "num_results": 3})
    assert secret_query in _result_text(result)
    assert srv._telemetry.flush()

    with sqlite3.connect(srv._telemetry.db_path) as conn:
        schema = "\n".join(
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        )
        values = "\n".join(
            str(value)
            for table in ("search_events", "provider_events")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row
        )
    persisted = f"{schema}\n{values}"
    assert secret_query not in persisted
    assert secret_result not in persisted
    lowered_schema = schema.lower()
    for forbidden in (
        "query",
        "full_url",
        "url_path",
        "title",
        "snippet",
        "content",
        "api_key",
        "credential_value",
    ):
        assert forbidden not in lowered_schema


@pytest.mark.asyncio
async def test_stats_aggregates_provider_states(monkeypatch, tmp_path):
    from types import SimpleNamespace

    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_telemetry", telemetry)

    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="brave", state="error", error="boom", attempts=1)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    await _call_tool("web_search", {"query": "not stored"})

    response = await srv.stats(SimpleNamespace(query_params={"window": "24h"}))
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["searches"]["total"] == 1
    assert payload["providers"]["brave"]["attempts"] == 1
    assert payload["providers"]["brave"]["errors"] == 1
    telemetry.close()


@pytest.mark.asyncio
async def test_stats_rejects_invalid_window():
    from types import SimpleNamespace

    response = await srv.stats(SimpleNamespace(query_params={"window": "1h"}))
    assert response.status_code == 400
    assert "24h, 7d, 30d" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_unavailable_telemetry_never_breaks_search(monkeypatch, tmp_path):
    from types import SimpleNamespace

    unavailable = srv.TelemetryStore(tmp_path / "disabled", enabled=False)
    monkeypatch.setattr(srv, "_telemetry", unavailable)

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": "B", "url": "https://example.com/", "snippet": "ok",
             "domain": "example.com", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "still works"})
    assert json.loads(_result_text(result))["status"] == "ok"
    response = await srv.stats(SimpleNamespace(query_params={"window": "24h"}))
    assert response.status_code == 503
    assert json.loads(response.body)["available"] is False


# --------------------------------------------------------------------------- #
# tools/list contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tools_list_exposes_expected_tools():
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {"web_search", "batch_web_search", "image_search", "web_fetch", "verify_url"}
    batch_tool = next(tool for tool in tools if tool.name == "batch_web_search")
    batch_schema = batch_tool.model_dump(by_alias=True)["inputSchema"]
    assert batch_schema["required"] == ["queries"]
    queries_schema = batch_schema["properties"]["queries"]
    assert queries_schema["type"] == "array"
    assert queries_schema["minItems"] == 1
    assert queries_schema["maxItems"] == srv.BATCH_MAX_QUERIES
    assert queries_schema["items"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": srv.BATCH_MAX_QUERY_CHARS,
    }
    assert batch_schema["properties"]["num_results"] == {"default": 8, "type": "integer"}
    expected_intent_schema = {
        "default": "general",
        "enum": ["general", "current", "news"],
        "type": "string",
    }
    assert batch_schema["properties"]["intent"] == expected_intent_schema
    web_tool = next(tool for tool in tools if tool.name == "web_search")
    web_schema = web_tool.model_dump(by_alias=True)["inputSchema"]
    assert web_schema["properties"]["intent"] == expected_intent_schema
    image_tool = next(tool for tool in tools if tool.name == "image_search")
    schema = image_tool.model_dump(by_alias=True)["inputSchema"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["num_results"] == {"default": 8, "type": "integer"}


# --------------------------------------------------------------------------- #
# Dedupe by URL.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dedupe_normalizes_host_identity_without_changing_url_semantics(monkeypatch):
    async def fake_brave_search(query, num_results, api_key):
        urls = [
            ("host", "HTTPS://Example.COM.:443/Case?keep=1&utm_source=x"),
            ("host duplicate", "https://example.com/Case?keep=1#fragment"),
            ("idna", "https://BÜCHER.example/Page"),
            ("idna duplicate", "https://xn--bcher-kva.EXAMPLE.:443/Page?fbclid=x"),
            ("path case", "https://example.com/case?keep=1"),
            ("query order one", "https://example.com/Case?a=1&a=2"),
            ("query order two", "https://example.com/Case?a=2&a=1"),
        ]
        return _brave_outcome([
            {
                "title": title,
                "url": url,
                "snippet": title,
                "domain": "unused.example",
                "engine": "brave",
                "provider": "brave",
                "score": None,
            }
            for title, url in urls
        ])

    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 8})
    payload = json.loads(_result_text(result))
    assert [item["title"] for item in payload["results"]] == [
        "host",
        "idna",
        "path case",
        "query order one",
        "query order two",
    ]
    assert [item["rank"] for item in payload["results"]] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# Circuit breaker.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_threshold(monkeypatch):
    """After N consecutive Brave failures, the breaker skips Brave."""
    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 2)
    monkeypatch.setattr(srv, "BREAKER_COOLDOWN", 60)
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    calls = {"n": 0}

    async def fake_brave_search(query, num_results, api_key):
        if not srv._breaker.allow("brave"):
            calls["n"]  # not contacted
            return srv._BackendOutcome(backend="brave", state="circuit_open", error="circuit open")
        calls["n"] += 1
        srv._breaker.record_failure("brave")
        return srv._BackendOutcome(backend="brave", state="error", error="boom")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    # Two failures to trip.
    await _call_tool("web_search", {"query": "q1"})
    await _call_tool("web_search", {"query": "q2"})
    assert calls["n"] == 2
    # Third call: breaker open → Brave not contacted.
    await _call_tool("web_search", {"query": "q3"})
    assert calls["n"] == 2  # not incremented


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(monkeypatch):
    """A success resets the failure counter."""
    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    state = {"fail": True}

    async def fake_brave_search(query, num_results, api_key):
        if state["fail"]:
            srv._breaker.record_failure("brave")
            return srv._BackendOutcome(backend="brave", state="error", error="boom")
        srv._breaker.record_success("brave")
        return _brave_outcome([
            {"title": "ok", "url": "https://x.example/", "snippet": "",
             "domain": "x.example", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    breaker = srv._breaker
    await _call_tool("web_search", {"query": "q"})  # fail 1
    assert breaker._fails.get("brave") == 1
    state["fail"] = False
    await _call_tool("web_search", {"query": "q"})  # success resets
    assert "brave" not in breaker._fails


# --------------------------------------------------------------------------- #
# Provider abstraction (ADR 0002 Phase 1).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_provider_list_default_order_and_names(monkeypatch):
    """The default provider list is Brave (primary, independent index)."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    names = [p.name for p in srv._PROVIDERS]
    assert names == ["brave"]
    assert all(p.output == "raw" for p in srv._PROVIDERS)




def test_default_timings_ms_has_all_provider_keys(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    timings = srv._default_timings_ms()
    assert set(timings) == {"total", "brave"}
    assert timings["total"] == 0.0
    assert timings["brave"] is None






@pytest.mark.asyncio
async def test_brave_provider_delegates_with_resolved_key(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    seen = {}

    async def fake_brave_search(query, num_results, api_key):
        seen["key"] = api_key
        return srv._BackendOutcome(backend="brave", ok=True, state="ok")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    await srv._BraveProvider().search("q", 5)
    assert seen["key"] == "brave-env"
    assert srv._BraveProvider.credential_label() == "keyed"


@pytest.mark.asyncio
async def test_web_search_result_carries_provider_field(monkeypatch):
    """Normalized results expose a 'provider' field naming the API backend."""
    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome([
            {"title": "T", "url": "https://example.com/a", "snippet": "c",
             "domain": "example.com", "engine": "brave", "provider": "brave", "score": None},
        ])
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["results"][0]["provider"] == "brave"
    assert payload["results"][0]["engine"] == "brave"
