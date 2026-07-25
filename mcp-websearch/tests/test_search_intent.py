"""Focused tests for explicit general/current/news search intent."""

from __future__ import annotations

import json
from typing import Any

import pytest

import server as srv


def _result(provider: str = "brave") -> dict[str, Any]:
    return {
        "title": "Result",
        "url": "https://example.com/result",
        "domain": "example.com",
        "snippet": "result",
        "engine": provider,
        "provider": provider,
        "score": None,
    }


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    yield
    telemetry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "expected_time_range"),
    [("general", None), ("current", "month"), ("news", "day")],
)
async def test_searxng_intent_sets_only_explicit_time_range(
    monkeypatch, intent, expected_time_range
):
    seen_params: dict[str, Any] = {}

    async def fake_request(path, params, timeout=None):
        seen_params.update(params)
        return {"results": [], "suggestions": []}

    monkeypatch.setattr(srv, "_searxng_request", fake_request)
    await srv._searxng_search("latest breaking news", 3, intent=intent)

    assert seen_params.get("time_range") == expected_time_range


class _IdentityResponse:
    status_code = 200
    headers = {"content-encoding": "identity"}

    def raise_for_status(self):
        return None

    async def aiter_raw(self):
        yield b'{"web":{"results":[]}}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "expected_freshness"),
    [("general", None), ("current", "pm"), ("news", "pd")],
)
async def test_brave_intent_sets_only_explicit_freshness(
    monkeypatch, intent, expected_freshness
):
    seen_params: dict[str, Any] = {}

    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            seen_params.update(params or {})
            return _IdentityResponse()

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(srv, "_client", fake_client)
    await srv._brave_search("latest breaking news", 3, "key", intent=intent)

    assert seen_params.get("freshness") == expected_freshness


@pytest.mark.asyncio
async def test_invalid_web_intent_returns_structured_error_without_provider_call(
    monkeypatch,
):
    called = False

    async def fake_brave_search(query, num_results, api_key):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv.web_search("query", intent="archive"))

    assert payload["status"] == "error"
    assert "intent must be one of" in payload["error"]
    assert payload["results"] == []
    assert called is False


@pytest.mark.asyncio
async def test_invalid_batch_intent_returns_structured_error_without_children(
    monkeypatch,
):
    called = False

    async def fake_search(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("batch child must not be called")

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    payload = json.loads(
        await srv.batch_web_search(["one", "two"], intent="archive")
    )

    assert payload["status"] == "error"
    assert "intent must be one of" in payload["error"]
    assert payload["results"] == []
    assert called is False


@pytest.mark.asyncio
async def test_batch_threads_explicit_intent_to_each_child(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_search(query, num_results=8, *, intent="general"):
        seen.append((query, intent))
        return json.dumps(
            {
                "query": query,
                "results": [],
                "suggestions": [],
                "text": "No results found",
                "status": "empty",
                "backend": "none",
                "attempted": [],
                "fallback_reason": None,
                "timings_ms": {"total": 1.0},
                "mode": "normal",
                "unresponsive_engines": [],
                "provider_states": {},
            }
        )

    monkeypatch.setattr(srv, "_web_search_impl", fake_search)
    await srv.batch_web_search(["one", "two"], intent="news")

    assert seen == [("one", "news"), ("two", "news")]


@pytest.mark.asyncio
async def test_intent_isolates_cache_variants_and_uses_short_ttl(monkeypatch):
    variants: list[str] = []
    news_flags: list[bool] = []

    class RecordingCache:
        def get_search(self, query, *, variant):
            variants.append(variant)
            return None

        def put_search(self, query, payload, *, variant, news=False):
            news_flags.append(news)

    cache = RecordingCache()

    async def fake_brave_search(query, num_results, api_key, intent="general"):
        return srv._BackendOutcome(
            backend="brave",
            ok=True,
            state="ok",
            results=[_result()],
            attempts=1,
        )

    monkeypatch.setattr(srv, "_get_cache", lambda: cache)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)

    general = json.loads(await srv._web_search_impl("same query", 1))
    current = json.loads(
        await srv._web_search_impl("same query", 1, intent="current")
    )
    news = json.loads(await srv._web_search_impl("same query", 1, intent="news"))

    assert len(set(variants)) == 3
    assert news_flags == [False, True, True]
    assert "intent" not in general
    assert "intent" not in current
    assert "intent" not in news
