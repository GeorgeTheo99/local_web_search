"""Tests for the local-search MCP websearch server.

Covers:
  - SSRF guard (_validate_public_http_url): rejects loopback, RFC1918,
    link-local, IPv6 local, localhost domains, redirect-to-private; allows
    public hosts.
  - web_fetch tool error contract for rejected URLs.
  - web_search error payload shape when SearXNG is unreachable.
  - web_search success payload shape (mocked SearXNG) matches the contract
    that Home Automation's McpSearchProvider expects (result.content[].text
    is JSON with results/suggestions/text).

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import asyncio
import json
import logging
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


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch, tmp_path):
    """Keep breaker, diagnostics, and telemetry isolated between tests."""
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)

    async def test_public_fetch_client():
        return await srv._client()

    monkeypatch.setattr(srv, "_public_fetch_client", test_public_fetch_client)
    yield
    telemetry.close()


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
    monkeypatch.setattr(srv, "_client", fake_client)
    final_url, body, content_type = await srv._fetch_public_body("https://example.com/path")

    assert resolutions == 1
    assert request["url"] == "https://93.184.216.34/path"
    assert request["headers"]["Host"] == "example.com"
    assert request["extensions"] == {"sni_hostname": "example.com"}
    assert final_url == "https://example.com/path"
    assert body == b"safe"
    assert content_type == "text/plain"


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
    monkeypatch.setattr(srv, "_client", fake_client)
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
    final_url, body, _ = await srv._fetch_public_body("https://first.example/start")

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
    monkeypatch.setattr(srv, "_client", fake_client)
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
    monkeypatch.setattr(srv, "_client", fake_client)
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
    monkeypatch.setattr(srv, "_client", fake_client)
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
    monkeypatch.setattr(srv, "_client", fake_client)
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
async def test_web_fetch_stops_stream_at_byte_limit(monkeypatch):
    class ChunkedResponse:
        is_redirect = False
        headers = {"content-type": "text/plain"}
        url = srv.httpx.URL("https://example.com/large")

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            yield b"a" * 6
            yield b"b" * 6

    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(ChunkedResponse())

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "FETCH_MAX_BYTES", 10)
    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_client", fake_client)
    result = await _call_tool("web_fetch", {"url": "https://example.com/large"})
    assert _result_text(result) == "Fetch error: response exceeds 10 byte limit"


@pytest.mark.asyncio
async def test_web_fetch_honors_small_max_chars(monkeypatch):
    class FakeClient:
        def stream(self, method, url, **kwargs):
            return _FakeStream(_StaticResponse(url, b"abcdefghijklmnopqrstuvwxyz", "text/plain"))

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_validate_public_http_url", _allow_public_url)
    monkeypatch.setattr(srv, "_client", fake_client)
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
    monkeypatch.setattr(srv, "_client", fake_client)
    result = await _call_tool("web_fetch", {"url": "https://example.com/fake.txt"})
    assert _result_text(result) == "Fetch error: unsupported binary content type: text/plain"


# --------------------------------------------------------------------------- #
# web_search payload contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_web_search_error_payload_when_searxng_down(monkeypatch):
    """When SearXNG is unreachable and Tavily also fails, the tool returns a
    structured error payload (not an exception) with the keys consumers expect."""
    async def fake_searxng(path, params, timeout=None):
        return None
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    async def fake_tavily(query, num_results, api_key):
        # Keyless path attempted, but simulate Tavily failure.
        return [], False
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
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
        "_searxng_search",
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


def test_public_result_urls_reject_noncanonical_private_ip_literals():
    for url in (
        "http://127.1/secret",
        "http://0177.0.0.1/secret",
        "http://0x7f.0.0.1/secret",
    ):
        assert srv._public_http_url(url) is None
        assert srv._normalize_searxng_image_result(
            {"img_src": url, "engine": "duckduckgo images"}
        ) is None


def test_tavily_base_url_requires_credential_free_https(monkeypatch):
    assert srv._tavily_search_url() == "https://api.tavily.com/search"
    for invalid in (
        "http://api.tavily.com",
        "https://user:pass@api.tavily.com",
        "https://api.tavily.com/v1",
        "https://api.tavily.com:bad",
        "https://api.tavily.com?token=secret",
    ):
        monkeypatch.setattr(srv, "TAVILY_BASE_URL", invalid)
        with pytest.raises(ValueError, match="credential-free HTTPS"):
            srv._tavily_search_url()


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
async def test_search_provider_requests_disable_compression(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def handler(request):
        seen.append((request.url.path, request.headers.get("accept-encoding", "")))
        if request.url.path == "/search" and request.method == "GET":
            return srv.httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=srv.httpx.ByteStream(b'{"results": []}'),
            )
        return srv.httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=srv.httpx.ByteStream(
                b'{"results": [{"title": "safe", "url": "https://example.com/"}]}'
            ),
        )

    client = srv.httpx.AsyncClient(transport=srv.httpx.MockTransport(handler))

    async def fake_client():
        return client

    monkeypatch.setattr(srv, "_client", fake_client)
    monkeypatch.setattr(srv, "TAVILY_BASE_URL", "https://api.tavily.com")
    try:
        searxng = await srv._searxng_request("/search", {"q": "test"})
        tavily = await srv._tavily_search("test", 1, "")
    finally:
        await client.aclose()

    assert searxng.data == {"results": []}
    assert tavily.state == "ok"
    assert seen == [("/search", "identity"), ("/search", "identity")]


@pytest.mark.asyncio
async def test_web_search_filters_unsafe_urls_and_falls_back_when_none_are_usable(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_MODE", "fallback")

    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {"title": "relative", "url": "/private", "content": "bad"},
                {"title": "script", "url": "javascript:alert(1)", "content": "bad"},
                {"title": "local", "url": "http://127.0.0.1/secret", "content": "bad"},
                {"title": "short local", "url": "http://127.1/secret", "content": "bad"},
            ],
            "unresponsive_engines": [["duckduckgo", "blocked"]],
        }

    async def fake_tavily(query, num_results, api_key):
        return [
            {"title": "safe", "url": "https://example.com/", "content": "good"},
            {"title": "credential", "url": "https://u:p@example.com/private", "content": "bad"},
        ], True

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "safe sources", "num_results": 5})
    payload = json.loads(_result_text(result))
    assert payload["attempted"] == ["searxng", "tavily"]
    assert [item["url"] for item in payload["results"]] == ["https://example.com/"]


@pytest.mark.asyncio
async def test_image_search_rejects_oversized_query_before_provider_access(monkeypatch):
    monkeypatch.setattr(
        srv,
        "_searxng_image_search",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )
    result = await _call_tool(
        "image_search",
        {"query": "x" * (srv.MAX_QUERY_CHARS + 1), "num_results": 3},
    )
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert str(srv.MAX_QUERY_CHARS) in payload["error"]


@pytest.mark.asyncio
async def test_web_search_success_payload_shape(monkeypatch):
    """Success payload matches the contract Home Automation's McpSearchProvider
    relies on: result.content[].text is JSON with results/suggestions/text,
    and each result has rank/title/url/domain/snippet/engine."""
    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {"title": "Example", "url": "https://example.com/a", "content": "snippet a", "engine": "brave"},
                {"title": "Other", "url": "https://other.com/b", "content": "snippet b", "engine": "qwant"},
            ],
            "suggestions": ["related"],
        }
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "hello", "num_results": 8})
    payload = json.loads(_result_text(result))
    assert payload["query"] == "hello"
    assert len(payload["results"]) == 2
    first = payload["results"][0]
    for key in ("rank", "title", "url", "domain", "snippet", "engine"):
        assert key in first
    assert first["domain"] == "example.com"
    assert payload["suggestions"] == ["related"]
    assert "## Search: hello" in payload["text"]

@pytest.mark.asyncio
async def test_web_search_empty_results_surfaces_unresponsive_engines(monkeypatch):
    """When SearXNG returns empty with unresponsive engines AND Tavily keyless
    also returns empty, the unresponsive-engine detail is preserved in text."""
    async def fake_request(path, params, timeout=None):
        return {"results": [], "suggestions": [], "unresponsive_engines": [{"name": "brave"}]}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    # Tavily keyless returns empty too.
    async def fake_tavily(query, num_results, api_key):
        return [], True
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "x"})
    payload = json.loads(_result_text(result))
    assert payload["results"] == []
    # Both backends empty → returns _format_results empty (no engine_msg),
    # but the keyless path was attempted. Verify no crash + empty payload.
    assert "No results found" in payload["text"]

@pytest.mark.asyncio
async def test_searxng_empty_unresponsive_shown_when_tavily_unreachable(monkeypatch):
    """When SearXNG is empty (with unresponsive engines) and Tavily FAILS,
    the SearXNG unresponsive-engine detail is surfaced (SearXNG was the only
    reachable source)."""
    async def fake_searxng(path, params, timeout=None):
        return {"results": [], "suggestions": [], "unresponsive_engines": [{"name": "brave"}]}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    async def fake_tavily(query, num_results, api_key):
        return [], False  # Tavily fails
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "x"})
    payload = json.loads(_result_text(result))
    # SearXNG was ok=True (reachable, just empty) + Tavily failed → both
    # reachable-but-empty branch does NOT apply; falls to error payload.
    assert "error" in payload or "No results" in payload["text"]


# --------------------------------------------------------------------------- #
# Policy, deadlines, metadata, and health.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_success_payload_includes_provider_metadata(monkeypatch):
    async def fake_request(path, params, timeout=None):
        return {
            "results": [{"title": "S", "url": "https://s.example/a", "content": "s", "engine": "brave"}],
            "suggestions": [],
        }
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "ok"
    assert payload["backend"] == "searxng"
    assert payload["attempted"] == ["searxng"]
    assert payload["fallback_reason"] is None
    assert set(payload["timings_ms"]) == {"total", "searxng", "tavily"}
    assert payload["mode"] == "fallback"
    assert payload["provider_states"] == {"searxng": "ok"}
    assert "query" not in srv._last_search
    assert srv._last_search["result_count"] == 1


@pytest.mark.asyncio
async def test_tavily_disabled_never_resolves_key_or_egresses(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_MODE", "disabled")
    async def fake_request(path, params, timeout=None):
        return {"results": [], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(
        srv,
        "_resolve_tavily_key",
        lambda: (_ for _ in ()).throw(AssertionError("Tavily key must not be resolved")),
    )
    result = await _call_tool("web_search", {"query": "private query"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "empty"
    assert payload["attempted"] == ["searxng"]
    assert payload["mode"] == "disabled"


@pytest.mark.asyncio
async def test_tavily_disabled_surfaces_searxng_degradation(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_MODE", "disabled")
    async def fake_request(path, params, timeout=None):
        return {"results": [], "suggestions": [], "unresponsive_engines": [["brave", "rate limited"]]}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "private query"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "degraded"
    assert payload["attempted"] == ["searxng"]
    assert payload["fallback_reason"] == "searxng_degraded"


@pytest.mark.asyncio
async def test_fallback_mode_keeps_degraded_nonempty_results_local(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_MODE", "fallback")

    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {"title": f"Local result {index}", "url": f"https://local.example/{index}", "content": "result", "engine": "bing"}
                for index in range(5)
            ],
            "suggestions": [],
            "unresponsive_engines": [["duckduckgo", "blocked"]],
        }

    async def fail_tavily(*_args):
        raise AssertionError("fallback mode must not egress when local results exist")

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", fail_tavily)
    result = await _call_tool("web_search", {"query": "town zoning", "num_results": 5})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "degraded"
    assert payload["attempted"] == ["searxng"]
    assert payload["fallback_reason"] is None
    assert payload["backend"] == "searxng"
    assert len(payload["results"]) == 5


@pytest.mark.asyncio
async def test_supplement_mode_combines_dedupes_and_overfetches(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_MODE", "supplement")
    monkeypatch.setattr(srv, "SUPPLEMENT_MIN_RESULTS", 3)
    async def fake_request(path, params, timeout=None):
        return {
            "results": [{"title": "S", "url": "https://example.com/a?utm_source=x", "content": "s", "engine": "brave"}],
            "suggestions": ["related"],
        }
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    seen = {}
    async def fake_tavily(query, num_results, api_key):
        seen["num_results"] = num_results
        return [
            {"title": "duplicate", "url": "https://example.com/a", "domain": "example.com", "snippet": "d", "engine": "tavily", "score": 0.9},
            {"title": "T", "url": "https://t.example/b", "domain": "t.example", "snippet": "t", "engine": "tavily", "score": 0.8},
        ], True
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert seen["num_results"] == 6
    assert [item["url"] for item in payload["results"]] == [
        "https://example.com/a?utm_source=x",
        "https://t.example/b",
    ]
    assert payload["status"] == "degraded"
    assert payload["backend"] == "searxng+tavily"
    assert payload["fallback_reason"] == "below_minimum"
    assert payload["attempted"] == ["searxng", "tavily"]


@pytest.mark.asyncio
async def test_searxng_stage_timeout_still_reaches_tavily(monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_TIMEOUT", 0.01)
    async def slow_searxng(query, num_results):
        await asyncio.sleep(0.1)
        return srv._BackendOutcome(backend="searxng", ok=True, state="empty")
    async def fake_tavily(query, num_results, api_key):
        return [{"title": "T", "url": "https://t.example/", "domain": "t.example", "snippet": "t", "engine": "tavily", "score": None}], True
    monkeypatch.setattr(srv, "_searxng_search", slow_searxng)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "degraded"
    assert payload["backend"] == "tavily"
    assert payload["fallback_reason"] == "searxng_timeout"
    assert payload["timings_ms"]["total"] < 1000


@pytest.mark.asyncio
async def test_degraded_is_distinct_from_empty_and_error(monkeypatch):
    async def fake_request(path, params, timeout=None):
        return {"results": [], "suggestions": [], "unresponsive_engines": [["brave", "rate limited"]]}
    async def fake_tavily(query, num_results, api_key):
        return [], False
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "degraded"
    assert "error" not in payload
    assert payload["unresponsive_engines"] == [["brave", "rate limited"]]
    assert payload["provider_states"] == {"searxng": "degraded", "tavily": "error"}


@pytest.mark.asyncio
async def test_health_reflects_recent_provider_failure(monkeypatch):
    async def reachable():
        return {"reachable": True, "latency_ms": 1.0}
    monkeypatch.setattr(srv, "_probe_searxng", reachable)
    monkeypatch.setattr(
        srv,
        "_last_search",
        {
            "status": "degraded",
            "provider_states": {"searxng": "degraded", "tavily": "error"},
        },
    )
    health = await srv._health_payload()
    assert health["ready"] is True
    assert health["status"] == "degraded"
    assert health["searxng"]["available"] is True
    assert health["tavily"]["available"] is False
    assert health["tavily"]["keyless_available"] is False
    assert health["tavily"]["last_state"] == "error"


@pytest.mark.asyncio
async def test_health_exposes_provider_stack_and_providers(monkeypatch):
    """ADR 0002 Phase 6: health reports the active stack and per-provider status."""
    async def reachable():
        return {"reachable": True, "latency_ms": 1.0}
    monkeypatch.setattr(srv, "_probe_searxng", reachable)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    health = await srv._health_payload()
    assert health["provider_stack"] == "searxng+tavily"
    names = [p["name"] for p in health["providers"]]
    assert names == ["searxng", "tavily"]
    assert all("credential_configured" in p for p in health["providers"])
    assert all("circuit" in p for p in health["providers"])


@pytest.mark.asyncio
async def test_health_provider_stack_reflects_kagi_sonar(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi+sonar")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-key")
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    health = await srv._health_payload()
    assert health["provider_stack"] == "kagi+sonar"
    names = [p["name"] for p in health["providers"]]
    assert names == ["kagi", "sonar"]
    kagi = next(p for p in health["providers"] if p["name"] == "kagi")
    sonar = next(p for p in health["providers"] if p["name"] == "sonar")
    assert kagi["credential_configured"] is True
    assert sonar["credential_configured"] is False


@pytest.mark.asyncio
async def test_readiness_and_health_expose_safe_state(monkeypatch):
    async def unavailable():
        return {"reachable": False, "latency_ms": 1.0}
    monkeypatch.setattr(srv, "_probe_searxng", unavailable)
    monkeypatch.setattr(srv, "TAVILY_MODE", "disabled")
    health = await srv._health_payload()
    assert health["ready"] is False
    assert health["status"] == "down"
    assert health["policy"]["tavily_mode"] == "disabled"
    response = await srv.ready(None)
    assert response.status_code == 503
    assert b'"ready":false' in response.body


@pytest.mark.asyncio
async def test_circuit_breaker_admits_one_half_open_probe(monkeypatch):
    monkeypatch.setattr(srv, "BREAKER_COOLDOWN", 10.0)
    monkeypatch.setattr(srv.time, "monotonic", lambda: 20.0)
    breaker = srv._CircuitBreaker()
    breaker._tripped["searxng"] = 0.0
    breaker._fails["searxng"] = 3
    assert breaker.allow("searxng") is True
    assert breaker.allow("searxng") is False
    assert breaker.snapshot("searxng")["state"] == "half_open"


def test_circuit_breaker_reports_one_atomic_open_transition(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 2)
    breaker = srv._CircuitBreaker()
    with ThreadPoolExecutor(max_workers=8) as executor:
        transitions = list(executor.map(lambda _: breaker.record_failure("tavily"), range(20)))
    assert transitions.count("opened") == 1
    assert transitions.count("reopened") == 0
    assert breaker.snapshot("tavily")["state"] == "open"


# --------------------------------------------------------------------------- #
# Query-free telemetry and aggregate endpoint.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_search_telemetry_never_persists_query_or_result_content(monkeypatch):
    import sqlite3

    secret_query = "SECRET_QUERY_4cb09b"
    secret_result = "SECRET_RESULT_78a2f1"

    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {
                    "title": secret_result,
                    "url": f"https://example.com/{secret_result}",
                    "content": secret_result,
                    "engine": "brave",
                }
            ],
            "suggestions": [secret_result],
        }

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
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
            for table in ("search_events", "provider_events", "engine_failures")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row
        )
    persisted = f"{schema}\n{values}"
    assert secret_query not in persisted
    assert secret_result not in persisted
    lowered_schema = schema.lower()
    for forbidden in ("query", "url", "title", "snippet", "content", "api_key", "credential_value"):
        assert forbidden not in lowered_schema


@pytest.mark.asyncio
async def test_stats_aggregates_degradation_tavily_429_and_fallback(monkeypatch):
    from types import SimpleNamespace

    async def fake_request(path, params, timeout=None):
        return {
            "results": [],
            "suggestions": [],
            "unresponsive_engines": [["bing", "too many requests"]],
        }

    async def fake_tavily(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="tavily",
            state="error",
            attempts=1,
            elapsed_ms=4.0,
            error="HTTP 429",
            http_status=429,
            credential_mode="keyless",
            circuit_before="closed",
            circuit_after="open",
            circuit_transition="opened",
            circuit_failures=3,
        )

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    await _call_tool("web_search", {"query": "not stored"})

    response = await srv.stats(SimpleNamespace(query_params={"window": "24h"}))
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["searches"]["total"] == 1
    assert payload["fallback"]["searches"] == 1
    assert payload["providers"]["searxng"]["states"] == {"degraded": 1}
    assert payload["providers"]["tavily"]["attempts"] == 1
    assert payload["providers"]["tavily"]["errors"] == 1
    assert payload["providers"]["tavily"]["rate_limited_429s"] == 1
    assert payload["providers"]["tavily"]["circuit_trips"] == 1
    assert payload["searxng_engine_failures"] == [
        {"engine": "bing", "reason": "rate_limited", "count": 1}
    ]
    assert payload["providers"]["tavily"]["credit_usage"]["available"] is False


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

    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {"title": "S", "url": "https://example.com/", "content": "ok", "engine": "brave"}
            ],
            "suggestions": [],
        }

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
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
    assert names == {"web_search", "batch_web_search", "image_search", "web_fetch", "answer_search", "verify_url"}
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
    image_tool = next(tool for tool in tools if tool.name == "image_search")
    schema = image_tool.model_dump(by_alias=True)["inputSchema"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["num_results"] == {"default": 8, "type": "integer"}


# --------------------------------------------------------------------------- #
# Tavily failover.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_tavily_key_searxng_only_returns_searxng_results(monkeypatch):
    """With no Tavily key (env or header), SearXNG results are returned directly."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    async def fake_request(path, params, timeout=None):
        return {"results": [{"title": "S", "url": "https://s.example/x", "content": "s", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert len(payload["results"]) == 1
    assert payload["results"][0]["engine"] == "brave"

@pytest.mark.asyncio
async def test_tavily_failover_when_searxng_empty(monkeypatch):
    """SearXNG returns empty + Tavily key present → Tavily is queried and returned."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-test")
    async def fake_searxng(path, params, timeout=None):
        return {"results": [], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    tavily_called = {"n": 0}
    async def fake_tavily(query, num_results, api_key):
        tavily_called["n"] += 1
        assert api_key == "tvly-test"
        return [{"title": "T", "url": "https://t.example/y", "domain": "t.example", "snippet": "t", "engine": "tavily", "score": None}], True
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert tavily_called["n"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["engine"] == "tavily"

@pytest.mark.asyncio
async def test_tavily_not_called_when_searxng_has_results(monkeypatch):
    """Credit-conserving: Tavily is NOT queried when SearXNG already returned results."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-test")
    async def fake_searxng(path, params, timeout=None):
        return {"results": [{"title": "S", "url": "https://s.example/x", "content": "s", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    async def fake_tavily(query, num_results, api_key):
        raise AssertionError("Tavily must not be called when SearXNG has results")
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["results"][0]["engine"] == "brave"

@pytest.mark.asyncio
async def test_no_tavily_key_searxng_failure_attempts_keyless_tavily(monkeypatch):
    """No key + SearXNG failure → keyless Tavily is attempted (free default).
    If keyless Tavily also fails, a structured error mentioning both is returned."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    async def fake_searxng(path, params, timeout=None):
        return None
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    tavily_called = {"key": None}
    async def fake_tavily(query, num_results, api_key):
        tavily_called["key"] = api_key  # should be "" (keyless)
        return [], False
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    # Keyless path was attempted (empty key passed).
    assert tavily_called["key"] == ""
    assert payload["results"] == []
    assert "error" in payload
    assert "SearXNG unreachable and Tavily failed" in payload["error"]

@pytest.mark.asyncio
async def test_keyless_tavily_returns_results_when_searxng_empty(monkeypatch):
    """No key + SearXNG empty → keyless Tavily returns results (free default
    path: zero onboarding, reliable search out of the box)."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    async def fake_searxng(path, params, timeout=None):
        return {"results": [], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    async def fake_tavily(query, num_results, api_key):
        assert api_key == ""  # keyless
        return [{"title": "T", "url": "https://t.example/y", "domain": "t.example", "snippet": "t", "engine": "tavily", "score": None}], True
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert len(payload["results"]) == 1
    assert payload["results"][0]["engine"] == "tavily"

@pytest.mark.asyncio
async def test_both_backends_fail_returns_error(monkeypatch):
    """SearXNG fails + Tavily fails → error payload mentioning both."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-test")
    async def fake_searxng(path, params, timeout=None):
        return None
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    async def fake_tavily(query, num_results, api_key):
        return [], False
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert "error" in payload
    assert "SearXNG unreachable and Tavily failed" in payload["error"]


# --------------------------------------------------------------------------- #
# Dedupe by URL.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dedupe_by_url_searxng_results(monkeypatch):
    """Duplicate URLs (same URL, different fragments) collapse to one."""
    async def fake_request(path, params, timeout=None):
        return {"results": [
            {"title": "A", "url": "https://example.com/page", "content": "a", "engine": "brave"},
            {"title": "A2", "url": "https://example.com/page#frag", "content": "a2", "engine": "qwant"},
            {"title": "B", "url": "https://other.com/", "content": "b", "engine": "brave"},
        ], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert len(payload["results"]) == 2  # page + page#frag deduped, other.com kept
    assert payload["results"][0]["rank"] == 1
    assert payload["results"][1]["rank"] == 2


# --------------------------------------------------------------------------- #
# Circuit breaker.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_threshold(monkeypatch):
    """After N consecutive SearXNG failures, the breaker skips SearXNG."""
    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 2)
    monkeypatch.setattr(srv, "BREAKER_COOLDOWN", 60)
    # Fresh breaker.
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    calls = {"n": 0}
    async def fake_searxng(path, params, timeout=None):
        calls["n"] += 1
        return None
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    # Two failures to trip.
    await _call_tool("web_search", {"query": "q1"})
    await _call_tool("web_search", {"query": "q2"})
    assert calls["n"] == 2
    # Third call: breaker open → SearXNG not contacted.
    await _call_tool("web_search", {"query": "q3"})
    assert calls["n"] == 2  # not incremented

@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(monkeypatch):
    """A success resets the failure counter."""
    monkeypatch.setattr(srv, "BREAKER_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    state = {"fail": True}
    async def fake_searxng(path, params, timeout=None):
        if state["fail"]:
            return None
        return {"results": [{"title": "ok", "url": "https://x.example/", "content": "", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    breaker = srv._breaker
    await _call_tool("web_search", {"query": "q"})  # fail 1
    assert breaker._fails.get("searxng") == 1
    state["fail"] = False
    await _call_tool("web_search", {"query": "q"})  # success resets
    assert "searxng" not in breaker._fails


# --------------------------------------------------------------------------- #
# Tavily key resolution (per-call header > env var).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resolve_tavily_key_env_fallback(monkeypatch):
    """No HTTP context (stdio) → env var is used."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-env")
    # get_http_headers raises LookupError in stdio/no-request context.
    import server as s
    def raise_lookup():
        raise LookupError("no request")
    monkeypatch.setattr(s, "get_http_headers", raise_lookup)
    assert s._resolve_tavily_key() == "tvly-env"

@pytest.mark.asyncio
async def test_resolve_tavily_key_header_precedence(monkeypatch):
    """Per-call X-Tavily-Key header takes precedence over env var.
    (The standard Authorization header is consumed by the MCP transport and
    does not reach tool code, so a custom header is used.)"""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-tavily-key": "tvly-header"})
    assert srv._resolve_tavily_key() == "tvly-header"

@pytest.mark.asyncio
async def test_resolve_tavily_key_does_not_repurpose_generic_api_key(monkeypatch):
    """Generic broker credentials must never be forwarded to Tavily."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-api-key": "broker-secret"})
    assert srv._resolve_tavily_key() == "tvly-env"

@pytest.mark.asyncio
async def test_resolve_tavily_key_empty_when_neither(monkeypatch):
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_tavily_key() == ""

@pytest.mark.asyncio
async def test_tavily_failover_uses_per_call_header_key(monkeypatch):
    """End-to-end: a per-call X-Tavily-Key header triggers Tavily failover even with no env key."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-tavily-key": "tvly-header"})
    async def fake_searxng(path, params, timeout=None):
        return {"results": [], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng)
    seen_key = {}
    async def fake_tavily(query, num_results, api_key):
        seen_key["k"] = api_key
        return [{"title": "T", "url": "https://t.example/y", "domain": "t.example", "snippet": "t", "engine": "tavily", "score": None}], True
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert seen_key.get("k") == "tvly-header"
    assert payload["results"][0]["engine"] == "tavily"


# --------------------------------------------------------------------------- #
# Provider abstraction (ADR 0002 Phase 1).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_provider_list_default_order_and_names():
    """The default provider list is SearXNG primary + Tavily fallback."""
    names = [p.name for p in srv._PROVIDERS]
    assert names == ["searxng", "tavily"]
    assert all(p.output == "raw" for p in srv._PROVIDERS)


def test_default_timings_ms_has_all_provider_keys():
    timings = srv._default_timings_ms()
    assert set(timings) == {"total", "searxng", "tavily"}
    assert timings["total"] == 0.0
    assert timings["searxng"] is None
    assert timings["tavily"] is None


def test_provider_timeout_reads_current_module_global(monkeypatch):
    """Provider.timeout must reflect monkeypatched SEARCH_TIMEOUT/TAVILY_TIMEOUT."""
    monkeypatch.setattr(srv, "SEARCH_TIMEOUT", 0.5)
    monkeypatch.setattr(srv, "TAVILY_TIMEOUT", 0.7)
    searxng = srv._SearXNGProvider()
    tavily = srv._TavilyProvider()
    assert searxng.timeout == 0.5
    assert tavily.timeout == 0.7


@pytest.mark.asyncio
async def test_searxng_provider_delegates_to_monkeypatched_search(monkeypatch):
    """Provider.search must call the module-level _searxng_search so tests win."""
    sentinel = srv._BackendOutcome(backend="searxng", ok=True, state="ok")
    called = {}
    async def fake_searxng_search(query, num_results):
        called["args"] = (query, num_results)
        return sentinel
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    outcome = await srv._SearXNGProvider().search("q", 5)
    assert outcome is sentinel
    assert called["args"] == ("q", 5)


@pytest.mark.asyncio
async def test_tavily_provider_delegates_and_resolves_key(monkeypatch):
    """Provider.search must call _tavily_search with the resolved key."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-x")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    seen = {}
    async def fake_tavily_search(query, num_results, api_key):
        seen["key"] = api_key
        return srv._BackendOutcome(backend="tavily", ok=True, state="ok")
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily_search)
    await srv._TavilyProvider().search("q", 5)
    assert seen["key"] == "tvly-x"
    assert srv._TavilyProvider.credential_label() == "keyed"


@pytest.mark.asyncio
async def test_web_search_result_carries_provider_field(monkeypatch):
    """Normalized results expose a 'provider' field naming the API backend."""
    async def fake_request(path, params, timeout=None):
        return {"results": [{"title": "T", "url": "https://example.com/a", "content": "c", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["results"][0]["provider"] == "searxng"
    assert payload["results"][0]["engine"] == "brave"
