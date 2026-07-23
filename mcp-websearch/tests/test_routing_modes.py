"""Tests for routing modes (ADR 0002 Phase 5).

Covers:
  - sensitive/no-egress: no external provider is contacted; the broker refuses
    with a structured error because no local corpus is configured. SearXNG is
    not used (it is not no-egress).
  - maximum_recall: opt-in serial escalation across every configured provider;
    results are merged and deduped.
  - normal mode (default) is unchanged.
  - WEBSEARCH_SEARCH_MODE default and per-call `mode` argument.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import json
import sqlite3
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
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    yield
    telemetry.close()


# --------------------------------------------------------------------------- #
# Sensitive / no-egress.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sensitive_mode_makes_no_external_call(monkeypatch):
    """sensitive mode must not contact any provider."""
    contacted = {"brave": False}

    async def fake_brave_search(query, num_results, api_key):
        contacted["brave"] = True
        return srv._BackendOutcome(backend="brave", ok=True, state="ok")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    result = await _call_tool("web_search", {"query": "private query", "mode": "sensitive"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "no local corpus" in payload["error"]
    assert payload["mode"] == "sensitive"
    assert payload["search_mode"] == "sensitive"
    assert contacted == {"brave": False}


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
async def test_maximum_recall_contacts_all_providers_in_stack(monkeypatch):
    """maximum_recall contacts every provider in the stack and merges results."""
    contacted = []

    async def fake_brave_search(query, num_results, api_key):
        contacted.append("brave")
        return srv._BackendOutcome(
            backend="brave", ok=True, state="ok", attempts=1,
            results=[{"title": "B", "url": "https://b.example/a", "domain": "b.example",
                      "snippet": "b", "engine": "brave", "provider": "brave", "score": None}],
        )
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    result = await _call_tool("web_search", {"query": "q", "mode": "maximum_recall"})
    payload = json.loads(_result_text(result))
    assert contacted == ["brave"]
    assert payload["attempted"] == ["brave"]
    assert payload["mode"] == "maximum_recall"
    assert payload["search_mode"] == "maximum_recall"
    urls = [r["url"] for r in payload["results"]]
    assert "https://b.example/a" in urls
    assert srv._telemetry.flush()
    with sqlite3.connect(srv._telemetry.db_path) as conn:
        persisted_mode = conn.execute(
            "SELECT mode FROM search_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert persisted_mode == "maximum_recall"


@pytest.mark.asyncio
async def test_maximum_recall_all_providers_fail_returns_structured_error(monkeypatch):
    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="brave",
            state="error",
            attempts=1,
            error="HTTP 503",
            http_status=503,
        )

    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    result = await _call_tool("web_search", {"query": "q", "mode": "maximum_recall"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert payload["error"] == "Search error: all configured providers failed."
    assert payload["text"] == payload["error"]
    assert payload["fallback_reason"] == "brave_error"
    assert payload["attempted"] == ["brave"]
    assert payload["estimated_cost_usd"] == 0.005


# --------------------------------------------------------------------------- #
# Normal mode default + env.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_normal_mode_default_is_unchanged(monkeypatch):
    """normal mode routes to the default (Brave) provider with no extra contact."""
    contacted = []

    async def fake_brave_search(query, num_results, api_key):
        contacted.append("brave")
        return srv._BackendOutcome(
            backend="brave", ok=True, state="ok", attempts=1,
            results=[
                {"title": "B", "url": "https://b.example/a", "domain": "b.example",
                 "snippet": "b", "engine": "brave", "provider": "brave", "score": None},
                {"title": "B2", "url": "https://b2.example/b", "domain": "b2.example",
                 "snippet": "b2", "engine": "brave", "provider": "brave", "score": None},
                {"title": "B3", "url": "https://b3.example/c", "domain": "b3.example",
                 "snippet": "b3", "engine": "brave", "provider": "brave", "score": None},
            ],
        )
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "brave"
    assert contacted == ["brave"]


def test_search_mode_env_default_is_normal():
    assert srv._SEARCH_MODE == "normal"
