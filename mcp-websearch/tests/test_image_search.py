"""Focused contract tests for the Brave-only image_search MCP tool."""

from __future__ import annotations

import json
from typing import Any

import pytest

import server as srv


async def _call_image_search(arguments: dict[str, Any]) -> dict[str, Any]:
    from fastmcp import Client

    async with Client(srv.mcp) as client:
        result = await client.call_tool("image_search", arguments)
    text = "\n".join(
        item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        for item in result.content
    )
    return json.loads(text)


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", tmp_path / "brave_key")
    yield
    telemetry.close()


@pytest.fixture
def brave_stack(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})


@pytest.mark.asyncio
async def test_brave_image_search_normalizes_results(monkeypatch, brave_stack):
    """Brave image search returns normalized image metadata and strict SafeSearch."""
    async def fake_brave_image_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="brave", ok=True, state="ok", attempts=1,
            results=[{
                "title": "Mountain landscape",
                "image_url": "https://images.example.com/mountain.jpg",
                "thumbnail_url": "https://imgs.search.brave.com/mountain.jpg",
                "page_url": "https://example.com/mountain",
                "source": "example.com",
                "engine": "brave",
                "width": 800,
                "height": 600,
                "mime_type": None,
                "creator": None,
                "license": None,
                "license_url": None,
            }],
        )
    monkeypatch.setattr(srv, "_brave_image_search", fake_brave_image_search)
    payload = await _call_image_search({"query": "mountain landscape", "num_results": 3})
    assert payload["status"] == "ok"
    assert payload["backend"] == "brave"
    assert payload["attempted"] == ["brave"]
    assert payload["safe_search"] == "strict"
    assert payload["estimated_cost_usd"] == 0.005
    assert len(payload["results"]) == 1
    img = payload["results"][0]
    assert img["image_url"] == "https://images.example.com/mountain.jpg"
    assert img["engine"] == "brave"
    assert img["width"] == 800
    assert img["height"] == 600


@pytest.mark.asyncio
async def test_brave_image_search_missing_key_returns_error(monkeypatch, brave_stack):
    """Brave image search without a key returns an error, no network call."""
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    called = {"network": False}

    class NoNetworkClient:
        async def stream(self, *a, **kw):
            called["network"] = True

    async def fake_client():
        return NoNetworkClient()
    monkeypatch.setattr(srv, "_client", fake_client)
    payload = await _call_image_search({"query": "test"})
    assert payload["status"] == "error"
    assert payload["attempted"] == []
    assert payload["safe_search"] == "strict"
    assert payload["estimated_cost_usd"] == 0.0
    assert called["network"] is False


def test_normalize_brave_image_result_extracts_fields():
    r = {
        "type": "image_result",
        "title": "Sunset",
        "url": "https://example.com/sunset",
        "source": "example.com",
        "thumbnail": {
            "src": "https://imgs.search.brave.com/sunset.jpg",
            "width": 494,
            "height": 350,
        },
        "properties": {
            "url": "https://static.example.com/sunset.jpg",
        },
    }
    out = srv._normalize_brave_image_result(r)
    assert out is not None
    assert out["image_url"] == "https://static.example.com/sunset.jpg"
    assert out["thumbnail_url"] == "https://imgs.search.brave.com/sunset.jpg"
    assert out["page_url"] == "https://example.com/sunset"
    assert out["source"] == "example.com"
    assert out["engine"] == "brave"
    assert out["width"] == 494
    assert out["height"] == 350


def test_normalize_brave_image_result_rejects_missing_image():
    r = {"title": "No image", "url": "https://example.com/page", "properties": {}}
    assert srv._normalize_brave_image_result(r) is None


def test_normalize_brave_image_result_rejects_private_url():
    r = {
        "title": "Private",
        "url": "https://example.com/page",
        "properties": {"url": "http://127.0.0.1/image.jpg"},
    }
    assert srv._normalize_brave_image_result(r) is None
