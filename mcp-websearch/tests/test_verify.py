"""Tests for the verify_url direct-fetch verifier.

verify_url is now a direct-fetch-only verifier (the same path as web_fetch).
Browser-based verification for pages that defeat a direct fetch is handled by
the Pi agent's shared browser_* tools, not by this broker.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import json
from typing import Any

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


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)


# --------------------------------------------------------------------------- #
# verify_url tool.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_verify_url_direct_success(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return "Real page content here."
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "direct"
    assert payload["error"] is None
    assert "Real page content" in payload["text"]


@pytest.mark.asyncio
async def test_verify_url_preserves_json_object_text(monkeypatch):
    document = json.dumps({"status": "ok", "value": 42})

    async def fake_web_fetch(url, max_chars=20000):
        return document

    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    result = await _call_tool("verify_url", {"url": "https://example.com/data.json"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "direct"
    assert payload["text"] == document
    assert payload["error"] is None


@pytest.mark.asyncio
async def test_verify_url_direct_fail_reports_error(monkeypatch):
    async def fake_web_fetch(url, max_chars=20000):
        return "Fetch error: request failed"
    monkeypatch.setattr(srv, "web_fetch", fake_web_fetch)
    result = await _call_tool("verify_url", {"url": "https://example.com/a"})
    payload = json.loads(_result_text(result))
    assert payload["method"] == "none"
    assert payload["text"] == ""
    assert "request failed" in payload["error"]
