"""Tests for the direct-fetch to Decodo universal scraper fallback."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import server as srv


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


def _http_error(status: int, url: str) -> srv.httpx.HTTPStatusError:
    request = srv.httpx.Request("GET", url)
    response = srv.httpx.Response(
        status, request=request, headers={"content-type": "text/html"}
    )
    return srv.httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


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


@pytest.mark.asyncio
async def test_403_uses_universal_premium_js_markdown_fallback(
    monkeypatch, isolated_fetch
):
    telemetry, _cache = isolated_fetch
    url = "https://example.com/blocked"
    content = ("Recovered public article content from Decodo. " * 8).strip()
    calls: list[dict[str, Any]] = []
    response = _StreamResponse(
        200,
        {"results": [{"content": content, "status_code": 200}]},
    )

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, calls),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.text == content
    assert result.provider == "decodo"
    assert result.content_type == "text/markdown"
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == srv.DECODO_API_URL
    assert call["headers"]["Authorization"] == "Basic test-decodo-token"
    assert call["headers"]["Accept-Encoding"] == "identity"
    assert call["json"] == {
        "url": url,
        "proxy_pool": "premium",
        "headless": "html",
        "markdown": True,
        "geo": "United States",
        "locale": "en-us",
        "device_type": "desktop",
    }

    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        attempt = conn.execute(
            "SELECT provider, outcome, trigger FROM fetch_events "
            "WHERE provider = 'decodo' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        operation = conn.execute(
            "SELECT outcome, provider, trigger FROM fetch_operations "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert attempt == ("decodo", "proxy_success", "http_403")
    assert operation == ("success", "decodo", "http_403")


@pytest.mark.asyncio
async def test_decodo_failure_preserves_direct_error(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    calls: list[dict[str, Any]] = []
    response = _StreamResponse(
        200,
        {"results": [{"content": "Payment required", "status_code": 402}]},
    )

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, calls),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 403 from {url}"
    assert result.provider == "none"
    assert len(calls) == 1

    telemetry = srv._get_telemetry()
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        attempt = conn.execute(
            "SELECT http_status, provider_http_status FROM fetch_events "
            "WHERE provider = 'decodo' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert attempt == (402, 200)
    assert telemetry.stats("24h")["fetches"]["attempts"]["decodo"][
        "payment_required_402s"
    ] == 0


@pytest.mark.asyncio
async def test_missing_or_insecure_key_skips_decodo(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    key_file = Path(srv.LOCAL_SEARCH_DATA_DIR) / "decodo_key"
    os.chmod(key_file, 0o644)

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    def unexpected_client(**_kwargs):
        pytest.fail("Decodo must not be called without an owner-only credential")

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv.httpx, "AsyncClient", unexpected_client)

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 403 from {url}"
    assert srv._resolve_decodo_key() == ""


@pytest.mark.asyncio
async def test_decodo_success_is_cached(monkeypatch, isolated_fetch):
    url = "https://example.com/challenge"
    content = "Cached Decodo article content. " * 8
    calls: list[dict[str, Any]] = []
    response = _StreamResponse(
        200,
        {"results": [{"content": content, "status_code": 200}]},
    )
    direct_calls = 0

    async def challenge_fetch(_url):
        nonlocal direct_calls
        direct_calls += 1
        return url, b"<html>Just a moment...</html>", "text/html", 200

    monkeypatch.setattr(srv, "_fetch_public_body", challenge_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, calls),
    )

    first = await srv._web_fetch_impl(url)
    second = await srv._web_fetch_impl(url)

    assert first.provider == "decodo"
    assert second.provider == "cache"
    assert second.cache_hit is True
    assert direct_calls == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_decodo_accepts_documented_outer_4xx_with_valid_result(
    monkeypatch, isolated_fetch
):
    url = "https://example.com/blocked"
    content = ("Valid recovered content inside a Decodo 4xx response. " * 6).strip()
    response = _StreamResponse(
        422,
        {"results": [{"content": content, "status_code": 200}]},
    )

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, []),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "decodo"
    assert result.text == content


@pytest.mark.asyncio
async def test_decodo_failure_falls_through_to_jina(monkeypatch, isolated_fetch):
    url = "https://example.com/blocked"
    response = _StreamResponse(
        200,
        {"results": [{"content": "Access denied", "status_code": 403}]},
    )
    jina_calls: list[tuple[str, int, str]] = []
    jina_content = "Jina recovered this public article after Decodo failed. " * 5

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    async def successful_jina(jina_url, max_chars, trigger="none", **_kwargs):
        jina_calls.append((jina_url, max_chars, trigger))
        return srv._FetchResult(
            jina_content, None, False, 0.0, jina_url, "text/markdown", "jina"
        )

    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv, "_jina_reader_fetch", successful_jina)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, []),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error is None
    assert result.provider == "jina"
    assert jina_calls == [(url, 50000, "http_403")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected_trigger"),
    [
        (429, None, "http_429"),
        (200, b"", "empty"),
    ],
)
async def test_decodo_receives_bounded_fallback_trigger(
    monkeypatch, isolated_fetch, status, body, expected_trigger
):
    url = "https://example.com/retry"
    content = "Recovered content for trigger telemetry. " * 6
    calls: list[dict[str, Any]] = []
    response = _StreamResponse(
        200,
        {"results": [{"content": content, "status_code": 200}]},
    )

    async def direct_fetch(_url):
        if body is None:
            raise _http_error(status, url)
        return url, body, "text/html", status

    monkeypatch.setattr(srv, "_fetch_public_body", direct_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, calls),
    )

    result = await srv._web_fetch_impl(url)

    assert result.provider == "decodo"
    telemetry = srv._telemetry
    assert telemetry is not None and telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        trigger = conn.execute(
            "SELECT trigger FROM fetch_events WHERE provider='decodo' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert trigger == expected_trigger


@pytest.mark.asyncio
async def test_decodo_rejects_encoded_or_oversized_responses_without_secret_leak(
    monkeypatch, isolated_fetch
):
    url = "https://example.com/blocked"

    async def blocked_fetch(_url):
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(srv, "DECODO_RESPONSE_MAX_BYTES", 32)
    response = _StreamResponse(
        200,
        {},
        headers={"content-encoding": "gzip"},
        raw_body=b"x" * 64,
    )
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, []),
    )

    result = await srv._web_fetch_impl(url)

    assert result.error == f"Fetch error: HTTP 403 from {url}"
    assert "test-decodo-token" not in result.text


@pytest.mark.asyncio
async def test_small_first_response_does_not_poison_fallback_cache(
    monkeypatch, isolated_fetch
):
    url = "https://example.com/long"
    content = "0123456789" * 200
    response = _StreamResponse(
        200,
        {"results": [{"content": content, "status_code": 200}]},
    )
    direct_calls = 0

    async def blocked_fetch(_url):
        nonlocal direct_calls
        direct_calls += 1
        raise _http_error(403, url)

    monkeypatch.setattr(srv, "_fetch_public_body", blocked_fetch)
    monkeypatch.setattr(
        srv.httpx,
        "AsyncClient",
        lambda **_kwargs: _DecodoClient(response, []),
    )

    first = await srv._web_fetch_impl(url, max_chars=120)
    second = await srv._web_fetch_impl(url, max_chars=2000)

    assert len(first.text) == 120
    assert second.text == content
    assert second.cache_hit is True
    assert direct_calls == 1


@pytest.mark.asyncio
async def test_fetch_operation_has_hard_timeout_and_preserves_cancellation(
    monkeypatch, isolated_fetch
):
    url = "https://example.com/slow"
    monkeypatch.setattr(srv, "FETCH_OPERATION_TIMEOUT", 0.01)

    async def slow_inner(_url, _max_chars=20000, _attempt_state=None, **_kwargs):
        assert _attempt_state is not None
        _attempt_state.update(provider="decodo", trigger="http_403")
        await srv.asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(srv, "_web_fetch_impl_inner", slow_inner)
    result = await srv._web_fetch_impl(url)
    assert "operation exceeded" in (result.error or "")
    telemetry = srv._telemetry
    assert telemetry is not None and telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        attempts = conn.execute(
            "SELECT provider, outcome FROM fetch_events ORDER BY id"
        ).fetchall()
    assert attempts == [("decodo", "timeout")]
    stats = telemetry.stats("24h")
    assert "none" not in stats["fetches"]["attempts"]
    assert stats["fetches"]["attempts"]["decodo"]["attempts"] == 1

    started = srv.asyncio.Event()

    async def cancellable_inner(_url, _max_chars=20000, _attempt_state=None, **_kwargs):
        started.set()
        await srv.asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(srv, "FETCH_OPERATION_TIMEOUT", 60)
    monkeypatch.setattr(srv, "_web_fetch_impl_inner", cancellable_inner)
    task = srv.asyncio.create_task(srv._web_fetch_impl(url))
    await started.wait()
    task.cancel()
    with pytest.raises(srv.asyncio.CancelledError):
        await task
