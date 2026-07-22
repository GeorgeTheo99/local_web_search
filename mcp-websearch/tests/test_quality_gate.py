"""Tests for the search quality gate (ADR 0002 Phase 3).

The gate evaluates the primary provider's deduped candidates in memory and
returns a query-free reason when they are thin, single-domain-dominated, or
duplicate/generic. It is enabled by default only for non-legacy stacks (auto);
the legacy searxng+tavily stack keeps it off so existing behavior is preserved.

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


def _result(url: str, snippet: str = "s", title: str = "T") -> dict[str, Any]:
    return {"title": title, "url": url, "domain": url.split("/")[2],
            "snippet": snippet, "engine": "kagi", "provider": "kagi", "score": None}


# --------------------------------------------------------------------------- #
# Gate unit tests (pure function).
# --------------------------------------------------------------------------- #

def test_quality_gate_empty_passes():
    passed, reason = srv._quality_gate([])
    assert passed is True
    assert reason is None


def test_quality_gate_below_min_results(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 3)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 1)
    cands = [_result("https://a.example/1")]
    passed, reason = srv._quality_gate(cands)
    assert passed is False
    assert reason == "quality_below_min_results"


def test_quality_gate_low_domain_diversity(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 2)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    cands = [
        _result("https://a.example/1", "alpha"),
        _result("https://a.example/2", "beta"),
        _result("https://a.example/3", "gamma"),
    ]
    passed, reason = srv._quality_gate(cands)
    assert passed is False
    assert reason == "quality_low_domain_diversity"


def test_quality_gate_duplicate_dominated(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 2)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    monkeypatch.setattr(srv, "QUALITY_DUPLICATE_FRACTION", 0.5)
    cands = [
        _result("https://a.example/1", "the quick brown fox jumps"),
        _result("https://b.example/2", "the quick brown fox jumps over"),
        _result("https://c.example/3", "the quick brown fox jumps over the"),
    ]
    passed, reason = srv._quality_gate(cands)
    assert passed is False
    assert reason == "quality_duplicate_dominated"


def test_quality_gate_passes_diverse_results(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 2)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    cands = [
        _result("https://a.example/1", "alpha bravo charlie"),
        _result("https://b.example/2", "delta echo foxtrot"),
        _result("https://c.example/3", "golf hotel india"),
    ]
    passed, reason = srv._quality_gate(cands)
    assert passed is True
    assert reason is None


def test_quality_gate_news_intent_stale(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 2)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    cands = [
        _result("https://a.example/1", "alpha bravo"),
        _result("https://b.example/2", "delta echo"),
    ]
    passed, reason = srv._quality_gate(cands, news_intent=True)
    assert passed is False
    assert reason == "quality_stale_for_news_intent"


def test_quality_gate_news_intent_fresh_passes(monkeypatch):
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 2)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    cands = [
        _result("https://a.example/1", "alpha (published 2026-07-20T00:00:00Z)"),
        _result("https://b.example/2", "delta (published 2026-07-21T00:00:00Z)"),
    ]
    passed, reason = srv._quality_gate(cands, news_intent=True)
    assert passed is True
    assert reason is None


# --------------------------------------------------------------------------- #
# Enablement rules.
# --------------------------------------------------------------------------- #

def test_quality_gate_auto_disabled_for_legacy_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+tavily")
    assert srv._quality_gate_enabled() is False


def test_quality_gate_auto_enabled_for_kagi_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi")
    assert srv._quality_gate_enabled() is True


def test_quality_gate_explicit_on_overrides_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "on")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+tavily")
    assert srv._quality_gate_enabled() is True


def test_quality_gate_explicit_off_overrides_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "off")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi")
    assert srv._quality_gate_enabled() is False


# --------------------------------------------------------------------------- #
# End-to-end: gate triggers fallback on a kagi stack with thin results.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_quality_gate_triggers_fallback_on_kagi_stack(monkeypatch):
    """Kagi stack + gate on + thin single-domain results -> fallback to Sonar.

    Phase 3 predates Sonar (Phase 4), so the fallback provider here is a stub
    installed via a second provider to prove the gate routes to it. We use the
    searxng+tavily stack with the gate forced on and a thin SearXNG result set
    so both providers exist and the gate is the only fallback trigger.
    """
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "on")
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 3)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    monkeypatch.setattr(srv, "TAVILY_MODE", "fallback")
    # SearXNG returns 1 result (1 domain): nonempty so policy says no fallback,
    # but the gate fails it.
    async def fake_request(path, params, timeout=None):
        return {"results": [{"title": "S", "url": "https://s.example/a",
                              "content": "s", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")

    async def fake_tavily(query, num_results, api_key):
        return ([{"title": "T", "url": "https://t.example/b", "domain": "t.example",
                  "snippet": "t", "engine": "tavily", "score": None}], True)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["attempted"] == ["searxng", "tavily"]
    assert payload["fallback_reason"] == "quality_below_min_results"
    # In fallback mode the low-quality primary results are replaced by Tavily's.
    assert payload["backend"] == "tavily"
    assert payload["results"][0]["url"] == "https://t.example/b"


@pytest.mark.asyncio
async def test_quality_gate_off_preserves_legacy_no_fallback(monkeypatch):
    """Legacy stack with gate auto/off: 1-result SearXNG set -> no fallback."""
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "TAVILY_MODE", "fallback")
    async def fake_request(path, params, timeout=None):
        return {"results": [{"title": "S", "url": "https://s.example/a",
                              "content": "s", "engine": "brave"}], "suggestions": []}
    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "TAVILY_API_KEY_ENV", "")
    called = {"tavily": False}
    async def fake_tavily(query, num_results, api_key):
        called["tavily"] = True
        return ([], False)
    monkeypatch.setattr(srv, "_tavily_search", fake_tavily)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "searxng"
    assert payload["fallback_reason"] is None
    assert called["tavily"] is False
