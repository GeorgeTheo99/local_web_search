"""Batch web-search contract, deadline, cancellation, and privacy tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import server as srv


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    from fastmcp import Client

    async with Client(srv.mcp) as client:
        return await client.call_tool(tool_name, arguments)


def _result_text(result: Any) -> str:
    content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    if isinstance(content, list):
        parts = [
            item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            for item in content
        ]
        return "\n".join(part for part in parts if isinstance(part, str))
    return json.dumps(result, default=str)


def _single_payload(query: str, *, status: str = "ok") -> str:
    return json.dumps(
        {
            "query": query,
            "results": [
                {
                    "rank": 1,
                    "title": query,
                    "url": f"https://example.com/{query}",
                    "domain": "example.com",
                    "snippet": "result",
                    "engine": "test",
                    "score": None,
                }
            ],
            "suggestions": [],
            "text": f"rendered text for {query}",
            "status": status,
            "backend": "searxng",
            "attempted": ["searxng"],
            "fallback_reason": None,
            "timings_ms": {"total": 1.0, "searxng": 1.0, "tavily": None},
            "mode": "fallback",
            "unresponsive_engines": [],
            "provider_states": {"searxng": "ok"},
        }
    )


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    yield telemetry
    telemetry.close()


@pytest.mark.asyncio
async def test_batch_search_deduplicates_preserves_order_and_compacts(monkeypatch):
    seen: list[str] = []

    async def fake_search(query: str, num_results: int = 8) -> str:
        seen.append(query)
        await asyncio.sleep(0)
        return _single_payload(query)

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    result = await _call_tool(
        "batch_web_search",
        {"queries": ["  Alpha   query ", "Beta", "alpha query"], "num_results": 4},
    )
    payload = json.loads(_result_text(result))

    assert seen == ["Alpha query", "Beta"]
    assert payload["status"] == "ok"
    assert payload["query_count"] == 2
    assert payload["duplicates_ignored"] == 1
    assert [item["query"] for item in payload["results"]] == ["Alpha query", "Beta"]
    assert all("text" not in item for item in payload["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queries", "message"),
    [
        ([], "must not be empty"),
        (["one", "two", "three", "four"], "at most 3"),
        (["one", "  "], "empty values"),
        (["x" * 513], "at most 512 characters"),
    ],
)
async def test_batch_search_rejects_invalid_input(queries, message):
    result = await _call_tool("batch_web_search", {"queries": queries})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert message in payload["error"]
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_batch_search_bounds_concurrency(monkeypatch):
    active = 0
    maximum = 0
    lock = asyncio.Lock()

    async def fake_search(query: str, num_results: int = 8) -> str:
        nonlocal active, maximum
        async with lock:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return _single_payload(query)

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    result = await _call_tool("batch_web_search", {"queries": ["one", "two", "three"]})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "ok"
    assert maximum == srv.BATCH_MAX_CONCURRENCY == 2


@pytest.mark.asyncio
async def test_batch_search_returns_partial_results_at_shared_deadline(
    monkeypatch, _reset_runtime_state
):
    monkeypatch.setattr(srv, "SEARCH_TOTAL_TIMEOUT", 0.05)

    async def fake_search(query: str, num_results: int = 8) -> str:
        if query == "fast":
            await asyncio.sleep(0.005)
            return _single_payload(query)
        await asyncio.sleep(1)
        return _single_payload(query)

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    started = srv.time.monotonic()
    result = await _call_tool("batch_web_search", {"queries": ["fast", "slow", "queued"]})
    elapsed = srv.time.monotonic() - started
    payload = json.loads(_result_text(result))

    assert elapsed < 0.5
    assert payload["status"] == "partial"
    assert [item["status"] for item in payload["results"]] == ["ok", "timeout", "timeout"]
    assert all("batch deadline exceeded" in item.get("error", "") for item in payload["results"][1:])
    assert all(item["fallback_reason"] == "batch_deadline" for item in payload["results"][1:])
    telemetry = _reset_runtime_state
    assert telemetry.flush(timeout=2)
    stats = telemetry.stats("24h")
    assert stats["searches"]["statuses"]["timeout"] == 2


@pytest.mark.asyncio
async def test_batch_search_cancels_children_when_caller_cancels(monkeypatch):
    started = asyncio.Event()
    cancelled = 0

    async def fake_search(query: str, num_results: int = 8) -> str:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled += 1
        return _single_payload(query)

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    task = asyncio.create_task(srv.batch_web_search(["one", "two", "three"]))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == srv.BATCH_MAX_CONCURRENCY


@pytest.mark.asyncio
async def test_batch_search_reuses_provider_policy_and_telemetry_stays_query_free(
    monkeypatch, _reset_runtime_state
):
    secrets = ["secret-alpha-9173", "secret-beta-4821"]

    async def fake_request(path, params, timeout=None):
        query = params["q"]
        return {
            "results": [
                {
                    "title": "Result",
                    "url": f"https://example.com/{query}",
                    "content": "ok",
                    "engine": "duckduckgo",
                }
            ],
            "suggestions": [],
        }

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    result = await _call_tool("batch_web_search", {"queries": secrets})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "ok"
    assert all(item["backend"] == "searxng" for item in payload["results"])

    telemetry = _reset_runtime_state
    assert telemetry.flush(timeout=2)
    database_bytes = telemetry.db_path.read_bytes()
    for secret in secrets:
        assert secret.encode() not in database_bytes
