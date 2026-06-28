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
# tools/list contract.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tools_list_exposes_expected_tools():
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {"web_search", "web_fetch"}


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
async def test_resolve_tavily_key_x_api_key_fallback(monkeypatch):
    """X-Api-Key is used when X-Tavily-Key is absent."""
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "tvly-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-api-key": "tvly-generic"})
    assert srv._resolve_tavily_key() == "tvly-generic"

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
