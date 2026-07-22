"""Tests for URL verification + Kagi Extract (ADR 0002 Phase 7).

Covers:
  - _kagi_extract: missing key short-circuits; successful response returns
    markdown; HTTP/timeout errors return structured failure; response bounded.
  - verify_url: direct fetch success returns method="direct"; direct fetch
    failure with a Kagi key falls back to Kagi Extract; both failing reports
    method="none" with the errors; no Kagi key returns the direct result/error.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import server as srv


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        return await client.call_tool(tool_name, arguments)


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None) if not isinstance(result, dict) else result.get("content")
    if isinstance(content, list):
        parts = [item.get("text") if isinstance(item, dict) else getattr(item, "text", None) for item in content]
        return "\n".join(p for p in parts if isinstance(p, str))
    return json.dumps(result, default=str)


def _async_client(client):
    async def _factory():
        return client
    return _factory


class _FakeStreamResponse:
    def __init__(self, data: dict[str, Any], status: int = 200):
        self._data = data
        self.status_code = status
        self.headers = {"content-encoding": "identity"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)

    async def aiter_raw(self):
        yield json.dumps(self._data).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_KAGI_SECRET_FILE", tmp_path / "kagi_key")


# --------------------------------------------------------------------------- #
# _kagi_extract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_kagi_extract_missing_key_short_circuits(monkeypatch):
    result = await srv._kagi_extract("https://example.com/a", "")
    assert result["ok"] is False
    assert result["error"] == "missing API key"


@pytest.mark.asyncio
async def test_kagi_extract_returns_markdown(monkeypatch):
    payload = {"meta": {"trace": "t", "ms": 100, "node": "us-east-1"},
               "data": [{"url": "https://example.com/a", "markdown": "# Title\n\nBody text."}]}

    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse(payload)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    result = await srv._kagi_extract("https://example.com/a", "kagi-key")
    assert result["ok"] is True
    assert result["markdown"] == "# Title\n\nBody text."
    assert result["error"] is None


@pytest.mark.asyncio
async def test_kagi_extract_timeout(monkeypatch):
    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    result = await srv._kagi_extract("https://example.com/a", "kagi-key")
    assert result["ok"] is False
    assert result["error"] == "request timed out"


@pytest.mark.asyncio
async def test_kagi_extract_http_error(monkeypatch):
    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse({}, status=403)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    result = await srv._kagi_extract("https://example.com/a", "kagi-key")
    assert result["ok"] is False
    assert result["error"] == "HTTP 403"


# --------------------------------------------------------------------------- #
# verify_url tool.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_verify_url_direct_success(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return json.dumps({"text": "Real page content here."})
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-key")
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "direct"
    assert payload["error"] is None
    assert "Real page content" in payload["text"]


@pytest.mark.asyncio
async def test_verify_url_falls_back_to_kagi_extract(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return "Fetch error: request failed"
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-key")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_kagi_extract(url, api_key):
        return {"ok": True, "markdown": "# Extracted\n\nContent from Kagi.", "error": None}
    monkeypatch.setattr(srv, "_kagi_extract", fake_kagi_extract)
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "kagi_extract"
    assert "Content from Kagi" in payload["text"]


@pytest.mark.asyncio
async def test_verify_url_both_fail_reports_errors(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return "Fetch error: HTTP 403"
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-key")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_kagi_extract(url, api_key):
        return {"ok": False, "markdown": "", "error": "HTTP 402"}
    monkeypatch.setattr(srv, "_kagi_extract", fake_kagi_extract)
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "none"
    assert "HTTP 403" in payload["error"]
    assert payload["extract_error"] == "HTTP 402"


@pytest.mark.asyncio
async def test_verify_url_direct_fail_no_kagi_key(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return "Fetch error: request failed"
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "none"
    assert "request failed" in payload["error"]
