"""Typed fallback eligibility matrix for the direct-fetch -> Decodo -> Jina path.

Covers every row of the eligibility matrix introduced alongside
``_UnsafeTargetError`` and ``_FALLBACK_HTTP_STATUSES``:

- eligible HTTP statuses (403/408/429/500/502/503/504 + CDN 52x) escalate;
- deterministic 4xx (400/401/404/410/422) do not;
- transport failures (timeout, connection, TLS) escalate after validation;
- HTML extraction failures escalate; PDF/binary failures do not;
- unsafe/private/unresolvable URLs fail closed (no proxy call);
- post-validation policy failures (oversize/encoding/redirect) do not escalate;
- the Decodo -> Jina order is preserved for new triggers;
- per-tier deadlines are clamped to the remaining shared operation budget.
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
from typing import Any

import pytest

import server as srv


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_fetch(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    cache = srv.WebCache(tmp_path / "cache")
    key_file = tmp_path / "decodo_key"
    key_file.write_text("test-decodo-token\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_cache", cache)
    monkeypatch.setattr(srv, "DECODO_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", False)
    yield telemetry, cache
    telemetry.close()
    cache.close()


def _http_status_error(status: int, url: str, *, content_type: str = "text/html") -> srv.httpx.HTTPStatusError:
    request = srv.httpx.Request("GET", url)
    response = srv.httpx.Response(
        status, request=request, headers={"content-type": content_type}
    )
    return srv.httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _StreamResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ):
        self.status_code = status_code
        self.request = srv.httpx.Request("POST", srv.DECODO_API_URL)
        self.headers = headers or {}
        self._body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_raw(self):
        yield self._body


class _DecodoClient:
    def __init__(self, response: _StreamResponse, calls: list[dict[str, Any]]):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def stream(self, method: str, url: str, **kwargs: Any):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def _decodo_success_client(calls: list[dict[str, Any]], content: str) -> _DecodoClient:
    response = _StreamResponse(
        200, {"results": [{"content": content, "status_code": 200}]}
    )
    return _DecodoClient(response, calls)


def _decodo_failure_client(calls: list[dict[str, Any]]) -> _DecodoClient:
    response = _StreamResponse(
        200, {"results": [{"content": "Access denied", "status_code": 403}]}
    )
    return _DecodoClient(response, calls)


def _install_decodo(monkeypatch, calls, *, succeed: bool, content: str = "Recovered matrix content. " * 12):
    client = _decodo_success_client(calls, content) if succeed else _decodo_failure_client(calls)
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **_kwargs: client)
    return client


# --------------------------------------------------------------------------- #
# Eligible HTTP statuses
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [403, 408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 527],
)
async def test_eligible_http_status_escalates_to_decodo(monkeypatch, isolated_fetch, status):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/edge"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)

    async def blocked_fetch(_url):
        raise _http_status_error(status, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(calls) == 1
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT provider, trigger FROM fetch_events WHERE provider='decodo' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("decodo", f"http_{status}")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 405, 410, 422])
async def test_deterministic_4xx_does_not_escalate(monkeypatch, isolated_fetch, status):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/missing"

    async def blocked_fetch(_url):
        raise _http_status_error(status, url)

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called for deterministic client errors")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP {status} from {url}"
    assert result.provider == "none"


@pytest.mark.asyncio
async def test_binary_error_body_does_not_escalate(monkeypatch, isolated_fetch):
    # A 503 with a binary content type is not eligible even though 503 is in the set.
    url = "https://example.com/binary-error"

    async def blocked_fetch(_url):
        raise _http_status_error(503, url, content_type="image/png")

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called for a binary error body")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 503 from {url}"
    assert result.provider == "none"


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_timeout_escalates_to_decodo(monkeypatch, isolated_fetch):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/slow"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)

    async def slow_fetch(_url):
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "_fetch_public_body", slow_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(calls) == 1
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT provider, outcome, trigger FROM fetch_events "
            "WHERE provider='direct' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("direct", "timeout", "timeout")


@pytest.mark.asyncio
async def test_connection_error_escalates_to_decodo(monkeypatch, isolated_fetch):
    url = "https://example.com/down"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)

    async def dead_fetch(_url):
        raise srv.httpx.ConnectError("connection refused", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "_fetch_public_body", dead_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tls_error_uses_tls_trigger(monkeypatch, isolated_fetch):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/bad-cert"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)
    request = srv.httpx.Request("GET", url)

    async def tls_fetch(_url):
        try:
            raise ssl.SSLError("certificate verify failed")
        except ssl.SSLError as exc:
            raise srv.httpx.ConnectError("tls handshake failed", request=request) from exc

    monkeypatch.setattr(srv, "_fetch_public_body", tls_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT outcome, trigger FROM fetch_events WHERE provider='direct' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("connection_error", "tls_error")


@pytest.mark.asyncio
async def test_outer_timeout_escalates_to_decodo(monkeypatch, isolated_fetch):
    url = "https://example.com/outer-slow"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)

    async def hang_fetch(_url):
        raise srv.asyncio.TimeoutError()

    monkeypatch.setattr(srv, "_fetch_public_body", hang_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Extraction failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_html_extraction_failure_escalates_to_decodo(monkeypatch, isolated_fetch):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/bad-html"
    calls: list[dict[str, Any]] = []
    _install_decodo(monkeypatch, calls, succeed=True)

    async def valid_html_fetch(_url):
        # >= 200 bytes of non-antibot HTML so the antibot/empty pre-check passes
        # and execution reaches the HTML extraction step, which then fails.
        body = (b"<html><body><p>" + b"Page content padding. " * 12 + b"</p></body></html>")
        return url, body, "text/html", 200

    async def failing_extract(_decoded, _current_url, _max_chars):
        raise srv.asyncio.TimeoutError()

    monkeypatch.setattr(srv, "_fetch_public_body", valid_html_fetch)
    monkeypatch.setattr(srv, "_extract_html_content_isolated", failing_extract)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(calls) == 1
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT provider, outcome, trigger FROM fetch_events "
            "WHERE provider='direct' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("direct", "extraction_error", "extraction_error")


@pytest.mark.asyncio
async def test_pdf_extraction_failure_does_not_escalate(monkeypatch, isolated_fetch):
    url = "https://example.com/broken.pdf"

    async def pdf_fetch(_url):
        return url, b"%PDF-1.4 broken", "application/pdf", 200

    async def failing_pdf(_body, _deadline):
        raise OSError("pdftotext failed")

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called for PDF extraction failures")

    monkeypatch.setattr(srv, "_fetch_public_body", pdf_fetch)
    monkeypatch.setattr(srv, "_extract_pdf_text", failing_pdf)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert "PDF extraction failed" in result.error
    # Direct-path extraction failures keep provider='direct'; the guarantee is
    # that no remote fallback tier was contacted.
    assert result.provider == "direct"


@pytest.mark.asyncio
async def test_binary_content_does_not_escalate(monkeypatch, isolated_fetch):
    url = "https://example.com/image.png"

    async def binary_fetch(_url):
        return url, b"\x89PNG\r\n\x1a\n", "image/png", 200

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called for binary content")

    monkeypatch.setattr(srv, "_fetch_public_body", binary_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert "unsupported binary" in result.error
    assert result.provider == "direct"


# --------------------------------------------------------------------------- #
# Fail-closed boundary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unsafe_private_url_fails_closed(monkeypatch, isolated_fetch):
    telemetry, _cache = isolated_fetch
    url = "https://private.example.local/secret"  # .localhost -> unsafe

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must never be called for an unsafe URL")

    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert result.provider == "none"
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        row = conn.execute(
            "SELECT outcome, trigger FROM fetch_events WHERE provider='direct' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("unsafe_url", "unsafe_url")


@pytest.mark.asyncio
async def test_policy_value_error_does_not_escalate(monkeypatch, isolated_fetch):
    # Oversize response is a post-validation policy failure -> no fallback.
    url = "https://example.com/huge"

    async def huge_fetch(_url):
        raise ValueError(f"response exceeds {srv.FETCH_MAX_BYTES} byte limit")

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called for a policy ValueError")

    monkeypatch.setattr(srv, "_fetch_public_body", huge_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert result.provider == "none"


@pytest.mark.asyncio
async def test_dns_resolution_timeout_fails_closed(monkeypatch, isolated_fetch):
    # A hung DNS lookup must surface as an unsafe/validation failure, not an
    # eligible network timeout. _resolve_host_ips is bypassed here by making
    # _fetch_public_body raise _UnsafeTargetError directly (as the validator does).
    url = "https://unresolvable.example.com/path"

    async def unresolvable_fetch(_url):
        raise srv._UnsafeTargetError("DNS resolution exceeded 8.0 second deadline")

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called when DNS validation did not complete")

    monkeypatch.setattr(srv, "_fetch_public_body", unresolvable_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert result.provider == "none"


# --------------------------------------------------------------------------- #
# Decodo -> Jina order for new triggers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_timeout_trigger_falls_through_to_jina(monkeypatch, isolated_fetch):
    url = "https://example.com/timeout-then-jina"
    jina_calls: list[tuple[str, int, str]] = []
    jina_content = "Jina recovered this after a direct timeout. " * 6

    async def successful_jina(jina_url, max_chars, trigger="none", **_kwargs):
        jina_calls.append((jina_url, max_chars, trigger))
        return srv._FetchResult(jina_content, None, False, 0.0, jina_url, "text/markdown", "jina")

    async def slow_fetch(_url):
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "_fetch_public_body", slow_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)
    _install_decodo(monkeypatch, [], succeed=False)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "jina"
    assert jina_calls == [(url, 50000, "timeout")]


@pytest.mark.asyncio
async def test_connection_error_trigger_falls_through_to_jina(monkeypatch, isolated_fetch):
    url = "https://example.com/conn-then-jina"
    jina_calls: list[tuple[str, int, str]] = []
    jina_content = "Jina recovered this after a connection error. " * 6

    async def successful_jina(jina_url, max_chars, trigger="none", **_kwargs):
        jina_calls.append((jina_url, max_chars, trigger))
        return srv._FetchResult(jina_content, None, False, 0.0, jina_url, "text/markdown", "jina")

    async def dead_fetch(_url):
        raise srv.httpx.ConnectError("refused", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "_fetch_public_body", dead_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)
    _install_decodo(monkeypatch, [], succeed=False)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "jina"
    assert jina_calls == [(url, 50000, "connection_error")]


# --------------------------------------------------------------------------- #
# Deadline budget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fallback_tier_timeout_clamped_to_remaining_budget(monkeypatch, isolated_fetch):
    # After a direct timeout consumes nearly the whole operation budget, the
    # Decodo tier must receive only the small remaining share, not its full
    # default 25s. We assert the clamped timeout passed to _decodo_scraper_fetch.
    url = "https://example.com/budget"
    received_timeouts: list[float] = []
    content = "Budgeted Decodo content. " * 12

    async def decodo_with_timeout(url_, max_chars, trigger="none", *, timeout=None):
        received_timeouts.append(timeout)
        return srv._FetchResult(content, None, False, 0.0, url_, "text/markdown", "decodo")

    async def slow_fetch(_url):
        # The direct fetch times out immediately, leaving most of a 2s operation
        # budget. That remainder (well under Decodo's 25s default) is what the
        # Decodo tier must be clamped to.
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "FETCH_OPERATION_TIMEOUT", 2.0)
    monkeypatch.setattr(srv, "DECODO_TIMEOUT", 25.0)
    monkeypatch.setattr(srv, "_fetch_public_body", slow_fetch)
    monkeypatch.setattr(srv, "_decodo_scraper_fetch", decodo_with_timeout)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert len(received_timeouts) == 1
    # The Decodo tier must be clamped to the remaining operation budget, which
    # is well below its 25s default.
    assert received_timeouts[0] is not None
    assert 1.0 <= received_timeouts[0] <= 2.0
    assert received_timeouts[0] < srv.DECODO_TIMEOUT


@pytest.mark.asyncio
async def test_decodo_tier_capped_so_jina_keeps_budget(monkeypatch, isolated_fetch):
    # With ample operation budget, Decodo must be held to its own tier timeout
    # rather than the whole remainder, so a stalled Decodo cannot starve Jina.
    url = "https://example.com/decodo-stall"
    decodo_timeouts: list[float] = []
    jina_timeouts: list[float] = []
    jina_content = "Jina recovered this after Decodo stalled. " * 6

    async def failing_decodo(url_, max_chars, trigger="none", *, timeout=None):
        decodo_timeouts.append(timeout)
        text = srv._fetch_error("Decodo request failed: TimeoutError")
        return srv._FetchResult(text, text, False, 0.0, url_, "", "decodo")

    async def successful_jina(url_, max_chars, trigger="none", *, timeout=None):
        jina_timeouts.append(timeout)
        return srv._FetchResult(jina_content, None, False, 0.0, url_, "text/markdown", "jina")

    async def slow_fetch(_url):
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "FETCH_OPERATION_TIMEOUT", 60.0)
    monkeypatch.setattr(srv, "DECODO_TIMEOUT", 25.0)
    monkeypatch.setattr(srv, "JINA_TIMEOUT", 30.0)
    monkeypatch.setattr(srv, "DECODO_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "_resolve_decodo_key", lambda: "test-decodo-token")
    monkeypatch.setattr(srv, "_fetch_public_body", slow_fetch)
    monkeypatch.setattr(srv, "_decodo_scraper_fetch", failing_decodo)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "jina"
    assert decodo_timeouts == [25.0]
    assert len(jina_timeouts) == 1
    assert 1.0 <= jina_timeouts[0] <= srv.JINA_TIMEOUT


@pytest.mark.asyncio
async def test_no_fallback_when_operation_budget_exhausted(monkeypatch, isolated_fetch):
    url = "https://example.com/no-budget"
    decodo_calls: list[float] = []

    async def decodo_never(url_, max_chars, trigger="none", *, timeout=None):
        decodo_calls.append(timeout)
        return srv._FetchResult("x", "x", False, 0.0, url_, "", "decodo")

    async def slow_fetch(_url):
        # The direct fetch times out; with a tiny operation budget the remaining
        # time falls below the 1s fallback threshold, so no tier is attempted.
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "FETCH_OPERATION_TIMEOUT", 0.5)
    monkeypatch.setattr(srv, "FETCH_TIMEOUT", 0.2)
    monkeypatch.setattr(srv, "_fetch_public_body", slow_fetch)
    monkeypatch.setattr(srv, "_decodo_scraper_fetch", decodo_never)

    result = await srv._web_fetch_impl(url)

    # The remaining operation budget is below the 1s fallback threshold, so the
    # direct timeout failure is returned without contacting any fallback tier.
    assert result.error is not None
    assert result.provider == "none"
    assert decodo_calls == []


# --------------------------------------------------------------------------- #
# Secret non-leakage on the new transport path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transport_fallback_does_not_leak_decodo_secret(monkeypatch, isolated_fetch):
    url = "https://example.com/secret-check"
    monkeypatch.setattr(srv, "DECODO_RESPONSE_MAX_BYTES", 32)
    response = _StreamResponse(
        200, {}, headers={"content-encoding": "gzip"}, raw_body=b"x" * 64,
    )
    monkeypatch.setattr(
        srv.httpx, "AsyncClient", lambda **_kwargs: _DecodoClient(response, [])
    )

    async def timeout_fetch(_url):
        raise srv.httpx.TimeoutException("read timeout", request=srv.httpx.Request("GET", url))

    monkeypatch.setattr(srv, "_fetch_public_body", timeout_fetch)

    result = await srv._web_fetch_impl(url)

    assert result.error is not None
    assert "test-decodo-token" not in result.text
