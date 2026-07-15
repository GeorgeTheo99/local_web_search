"""Focused contract tests for the loopback-only image_search MCP tool."""

from __future__ import annotations

import json
import sqlite3
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
    yield
    telemetry.close()


@pytest.mark.asyncio
async def test_image_search_request_params_and_normalization(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_request(path, params, timeout=None):
        seen.update(path=path, params=params, timeout=timeout)
        return {
            "results": [
                {
                    "title": "  A public image  ",
                    "url": "https://example.com/source",
                    "img_src": "https://cdn.example.com/image.jpg",
                    "thumbnail_src": "javascript:alert(1)",
                    "source": "Example Images",
                    "engine": "duckduckgo images",
                    "resolution": "1920 × 1080",
                    "img_format": "JPG",
                    "author": "Ada Creator",
                    "license_name": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                },
                {
                    "title": "Private image URL",
                    "url": "https://example.com/private",
                    "img_src": "http://127.0.0.1/private.jpg",
                    "engine": "duckduckgo images",
                },
                {
                    "title": "Engine without verified SafeSearch",
                    "url": "https://example.com/unverified",
                    "img_src": "https://cdn.example.com/unverified.jpg",
                    "engine": "openverse",
                },
            ],
            "suggestions": ["related image"],
        }

    async def forbidden_tavily(*args, **kwargs):
        raise AssertionError("image_search must never call Tavily")

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", forbidden_tavily)
    monkeypatch.setattr(
        srv,
        "_resolve_tavily_key",
        lambda: (_ for _ in ()).throw(AssertionError("Tavily key must not be resolved")),
    )
    payload = await _call_image_search({"query": "  red fox  ", "num_results": 8})

    assert seen == {
        "path": "/search",
        "params": {
            "q": "red fox",
            "format": "json",
            "categories": "images",
            "safesearch": 1,
        },
        "timeout": None,
    }
    assert payload["status"] == "ok"
    assert payload["backend"] == "searxng"
    assert payload["safe_search"] == "moderate"
    assert payload["attempted"] == ["searxng"]
    assert payload["mode"] == "disabled"
    assert payload["suggestions"] == ["related image"]
    assert payload["results"] == [
        {
            "rank": 1,
            "title": "A public image",
            "image_url": "https://cdn.example.com/image.jpg",
            "thumbnail_url": None,
            "page_url": "https://example.com/source",
            "source": "Example Images",
            "engine": "duckduckgo images",
            "width": 1920,
            "height": 1080,
            "mime_type": "image/jpeg",
            "creator": "Ada Creator",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        }
    ]


@pytest.mark.asyncio
async def test_image_search_dedupes_before_limit_and_clamps_to_max(monkeypatch):
    raw_results = [
        {
            "title": "First",
            "url": "https://page.example/first",
            "img_src": "https://img.example/photo.jpg?utm_source=one#top",
            "engine": "duckduckgo images",
        },
        {
            "title": "Duplicate",
            "url": "https://page.example/duplicate",
            "img_src": "https://img.example/photo.jpg",
            "engine": "google images",
        },
        *[
            {
                "title": f"Image {index}",
                "url": f"https://page.example/{index}",
                "img_src": f"https://img.example/{index}.jpg",
                "engine": "duckduckgo images",
            }
            for index in range(1, 25)
        ],
    ]

    async def fake_request(path, params, timeout=None):
        return {"results": raw_results, "suggestions": []}

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    limited = await _call_image_search({"query": "q", "num_results": 2})
    assert [item["title"] for item in limited["results"]] == ["First", "Image 1"]
    assert [item["rank"] for item in limited["results"]] == [1, 2]

    clamped = await _call_image_search({"query": "q", "num_results": 999})
    assert len(clamped["results"]) == srv.MAX_NUM_RESULTS
    assert clamped["results"][-1]["rank"] == srv.MAX_NUM_RESULTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        ({"results": [], "suggestions": []}, "empty"),
        (
            {
                "results": [],
                "suggestions": [],
                "unresponsive_engines": [["bing images", "timeout"]],
            },
            "degraded",
        ),
        (None, "error"),
    ],
)
async def test_image_search_empty_degraded_and_error_without_tavily(
    monkeypatch, response, expected_status
):
    async def fake_request(path, params, timeout=None):
        return response

    async def forbidden_tavily(*args, **kwargs):
        raise AssertionError("image_search must never call Tavily")

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    monkeypatch.setattr(srv, "_tavily_search", forbidden_tavily)
    payload = await _call_image_search({"query": "q"})

    assert payload["status"] == expected_status
    assert payload["results"] == []
    assert payload["attempted"] == ["searxng"]
    assert payload["timings_ms"]["tavily"] is None
    assert payload["provider_states"]["searxng"] in {expected_status, "error"}
    assert ("error" in payload) is (expected_status == "error")


@pytest.mark.asyncio
async def test_image_search_rejects_non_loopback_searxng_without_request(monkeypatch):
    monkeypatch.setattr(srv, "SEARXNG_URL", "https://search.example.com")

    async def forbidden_request(*args, **kwargs):
        raise AssertionError("non-loopback SearXNG must not be contacted")

    monkeypatch.setattr(srv, "_searxng_request", forbidden_request)
    payload = await _call_image_search({"query": "q"})
    assert payload["status"] == "error"
    assert "loopback SearXNG is required" in payload["error"]


@pytest.mark.asyncio
async def test_image_search_telemetry_persists_no_query_or_result_metadata(monkeypatch):
    secret_query = "SECRET_IMAGE_QUERY_124b"
    secret_result = "SECRET_IMAGE_RESULT_98ea"

    async def fake_request(path, params, timeout=None):
        return {
            "results": [
                {
                    "title": secret_result,
                    "url": f"https://page.example/{secret_result}",
                    "img_src": f"https://img.example/{secret_result}.jpg",
                    "engine": "google images",
                }
            ]
        }

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    payload = await _call_image_search({"query": secret_query})
    assert payload["results"]
    assert srv._telemetry.flush()
    assert "query" not in srv._last_search
    assert "results" not in srv._last_search

    with sqlite3.connect(srv._telemetry.db_path) as conn:
        persisted = "\n".join(
            str(value)
            for table in ("search_events", "provider_events", "engine_failures")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row
        )
    assert secret_query not in persisted
    assert secret_result not in persisted
