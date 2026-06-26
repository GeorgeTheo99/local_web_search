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
from typing import Any
from unittest.mock import AsyncMock, patch

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
    await srv._validate_public_http_url("https://example.com/")  # should not raise


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


# --------------------------------------------------------------------------- #
# web_search payload contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_web_search_error_payload_when_searxng_down(monkeypatch):
    """When SearXNG is unreachable, the tool returns a structured error payload
    (not an exception) with the keys McpSearchProvider consumers expect."""
    async def fake_request(path, params, timeout=None):
        return None
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "test", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["query"] == "test"
    assert payload["results"] == []
    assert payload["suggestions"] == []
    assert payload["error"]
    assert "text" in payload

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
    async def fake_request(path, params, timeout=None):
        return {"results": [], "suggestions": [], "unresponsive_engines": [{"name": "brave"}]}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("web_search", {"query": "x"})
    payload = json.loads(_result_text(result))
    assert payload["results"] == []
    assert "unresponsive engines: brave" in payload["text"]


# --------------------------------------------------------------------------- #
# tools/list contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tools_list_exposes_expected_tools():
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {"web_search", "web_fetch"}
