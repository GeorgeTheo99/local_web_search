"""Tests for routing modes (ADR 0002 Phase 5).

Covers:
  - sensitive/no-egress: no external provider is contacted; the broker refuses
    with a structured error because no local corpus is configured. SearXNG is
    not used (it is not no-egress).
  - maximum_recall: opt-in serial escalation across every configured provider;
    results are merged and deduped; a Sonar answer is surfaced separately.
  - normal mode (default) is unchanged.
  - WEBSEARCH_SEARCH_MODE default and per-call `mode` argument.

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


# --------------------------------------------------------------------------- #
# Sensitive / no-egress.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sensitive_mode_makes_no_external_call(monkeypatch):
    """sensitive mode must not contact any provider, including SearXNG."""
    contacted = {"searxng": False, "tavily": False}

    async def fake_searxng_request(path, params, timeout=None):
        contacted["searxng"] = True
        return {"results": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_searxng_request)

    async def fake_tavily(query, num_results, api_key):
        contacted["tavily"] = True
        return ([], False)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")

    result = await _call_tool("web_search", {"query": "private query", "mode": "sensitive"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "no local corpus" in payload["error"]
    assert payload["search_mode"] == "sensitive"
    assert contacted == {"searxng": False, "tavily": False}


@pytest.mark.asyncio
async def test_sensitive_mode_empty_query_still_errors(monkeypatch):
    result = await _call_tool("web_search", {"query": "", "mode": "sensitive"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "empty" in payload["error"]


# --------------------------------------------------------------------------- #
# Maximum recall.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_maximum_recall_serially_escalates_all_providers(monkeypatch):
    """maximum_recall contacts every provider in the stack and merges results."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+tavily")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    contacted = []

    async def fake_request(path, params, timeout=None):
        contacted.append("searxng")
        return {"results": [{"title": "S", "url": "https://s.example/a",
                              "content": "s", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")

    async def fake_tavily(query, num_results, api_key):
        contacted.append("tavily")
        return ([{"title": "T", "url": "https://t.example/b", "domain": "t.example",
                  "snippet": "t", "engine": "tavily", "score": None}], True)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)

    result = await _call_tool("web_search", {"query": "q", "mode": "maximum_recall"})
    payload = json.loads(_result_text(result))
    assert contacted == ["searxng", "tavily"]  # serial, both attempted
    assert payload["attempted"] == ["searxng", "tavily"]
    assert payload["search_mode"] == "maximum_recall"
    urls = [r["url"] for r in payload["results"]]
    assert "https://s.example/a" in urls
    assert "https://t.example/b" in urls


@pytest.mark.asyncio
async def test_maximum_recall_kagi_sonar_surfaces_answer(monkeypatch):
    """maximum_recall on kagi+sonar merges results and surfaces Sonar's answer."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi+sonar")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-key")
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "px-key")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_kagi_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="kagi", ok=True, state="ok",
            results=[{"title": "K", "url": "https://k.example/", "domain": "k.example",
                      "snippet": "k", "engine": "kagi", "provider": "kagi", "score": None}],
        )
    monkeypatch.setattr(srv, "_kagi_search", fake_kagi_search)

    async def fake_sonar_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="sonar", ok=True, state="ok", answer="Merged answer.",
            citations=["https://s.example/cite"],
            results=[{"title": "S", "url": "https://s.example/a", "domain": "s.example",
                      "snippet": "s", "engine": "sonar", "provider": "sonar", "score": None}],
        )
    monkeypatch.setattr(srv, "_sonar_search", fake_sonar_search)

    result = await _call_tool("web_search", {"query": "q", "mode": "maximum_recall"})
    payload = json.loads(_result_text(result))
    assert payload["attempted"] == ["kagi", "sonar"]
    assert payload["answer"] == "Merged answer."
    assert payload["citations"] == ["https://s.example/cite"]
    urls = [r["url"] for r in payload["results"]]
    assert "https://k.example/" in urls
    assert "https://s.example/a" in urls


# --------------------------------------------------------------------------- #
# Normal mode default + env.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_normal_mode_default_is_unchanged(monkeypatch):
    """normal mode still routes SearXNG-first with no extra provider contact."""
    contacted = []

    async def fake_request(path, params, timeout=None):
        contacted.append("searxng")
        return {"results": [{"title": "S", "url": "https://s.example/a",
                              "content": "s", "engine": "brave"},
                             {"title": "S2", "url": "https://s2.example/b",
                              "content": "s2", "engine": "brave"},
                             {"title": "S3", "url": "https://s3.example/c",
                              "content": "s3", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")

    async def fake_tavily(query, num_results, api_key):
        contacted.append("tavily")
        return ([], False)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)

    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "searxng"
    assert contacted == ["searxng"]  # Tavily not contacted in normal mode on success


def test_search_mode_env_default_is_normal():
    assert srv._SEARCH_MODE == "normal"
