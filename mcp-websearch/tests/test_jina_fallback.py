"""Tests for the direct-fetch to Jina Reader escalation path."""

from __future__ import annotations

import sqlite3

import pytest

import server as srv


@pytest.fixture
def isolated_fetch(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    cache = srv.WebCache(tmp_path / "cache")
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_cache", cache)
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    yield
    telemetry.close()
    cache.close()


def _http_error(status: int, url: str) -> srv.httpx.HTTPStatusError:
    request = srv.httpx.Request("GET", url)
    response = srv.httpx.Response(status, request=request)
    return srv.httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


class _JinaResponse:
    status_code = 200

    def __init__(self, content: str):
        self.content = content.encode("utf-8")
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_raw(self):
        yield self.content


class _JinaClient:
    def __init__(self, response: _JinaResponse, calls: list[tuple[str, dict[str, str]]]):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        assert method == "GET"
        self.calls.append((url, headers))
        return self.response


@pytest.mark.asyncio
async def test_403_direct_fetch_triggers_jina_fallback(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    direct_calls = 0
    jina_calls: list[tuple[str, dict[str, str]]] = []
    markdown = ("Jina extracted article content. " * 8).strip()
    jina_response = _JinaResponse(
        "Title: Example\n\nURL Source: https://example.com/blocked\n\n"
        f"Markdown Content:\n{markdown}"
    )

    async def blocked_fetch(_url):
        nonlocal direct_calls
        direct_calls += 1
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _JinaClient(jina_response, jina_calls),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.text == markdown
    assert result.content_type == "text/markdown"
    assert direct_calls == 1
    assert jina_calls == [
        (
            f"https://r.jina.ai/{url}",
            {"Accept": "text/plain", "Accept-Encoding": "identity"},
        )
    ]
    assert srv._telemetry is not None and srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        assert conn.execute(
            "SELECT outcome, tier_used FROM fetch_events ORDER BY id DESC LIMIT 1"
        ).fetchone() == ("proxy_success", "proxy")


@pytest.mark.asyncio
async def test_real_200_content_does_not_trigger_jina(monkeypatch, isolated_fetch):
    url = "https://example.com/ok"
    direct_calls = 0

    async def successful_fetch(_url):
        nonlocal direct_calls
        direct_calls += 1
        return url, b"real page content " * 20, "text/plain", 200

    async def unexpected_jina(_url, _max_chars, _trigger="none"):
        pytest.fail("Jina should not be called for real direct content")

    monkeypatch.setattr(srv, "_fetch_public_body", successful_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", unexpected_jina)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.text == "real page content " * 20
    assert direct_calls == 1


@pytest.mark.asyncio
async def test_antibot_direct_page_triggers_jina_fallback(monkeypatch, isolated_fetch):
    url = "https://example.com/challenge"
    content = "Reader content from the anti-bot recovery path. " * 5

    async def challenge_fetch(_url):
        return url, b"<html><title>Just a moment...</title></html>", "text/html", 200

    async def successful_jina(_url, _max_chars, _trigger="none"):
        return srv._FetchResult(content, None, False, 0.0, url, "text/markdown", "jina")

    monkeypatch.setattr(srv, "_fetch_public_body", challenge_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.text == content
    assert result.content_type == "text/markdown"


@pytest.mark.asyncio
async def test_disabled_jina_fallback_skips_escalation(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", False)

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    async def unexpected_jina(_url, _max_chars, _trigger="none"):
        pytest.fail("disabled Jina fallback must not be called")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", unexpected_jina)

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 403 from {url}"


def test_antibot_page_detection_handles_cloudflare_and_datadome_markers():
    assert srv._looks_like_antibot_page("<html>Just a moment... cf-browser-verification")
    assert srv._looks_like_antibot_page("Performing security verification for Human Verification")
    assert srv._looks_like_antibot_page("DataDome challenge: captcha")
    assert srv._looks_like_antibot_page("Access Denied — Pardon Our Interruption")
    assert not srv._looks_like_antibot_page("<html><body>Actual article content</body></html>")


@pytest.mark.asyncio
async def test_jina_failure_returns_original_direct_error(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    direct_error = _http_error(403, url)

    async def blocked_fetch(_url):
        raise direct_error

    async def failed_jina(_url, _max_chars, _trigger="none"):
        message = "Fetch error: Jina Reader request failed"
        return srv._FetchResult(message, message, False, 0.0, url, "", "jina")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", failed_jina)

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 403 from {url}"


@pytest.mark.asyncio
async def test_jina_result_is_cached(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    direct_calls = 0
    jina_calls = 0
    content = "Cached Jina article content. " * 8

    async def blocked_fetch(_url):
        nonlocal direct_calls
        direct_calls += 1
        raise _http_error(403, url)

    async def successful_jina(_url, _max_chars, _trigger="none"):
        nonlocal jina_calls
        jina_calls += 1
        return srv._FetchResult(content, None, False, 0.0, url, "text/markdown", "jina")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)

    first = await srv._web_fetch_impl(url)
    second = await srv._web_fetch_impl(url)

    assert first.error is None
    assert second.error is None
    assert second.text == content
    assert second.cache_hit is True
    assert direct_calls == 1
    assert jina_calls == 1
