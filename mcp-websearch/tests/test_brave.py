"""Tests for the Brave Search provider (ADR 0002 Brave addition).

Covers:
  - _normalize_brave_result: web results normalized with provider="brave";
    private/non-http URLs rejected; published timestamps preserved as a
    snippet hint.
  - _resolve_brave_key: X-Brave-Key header > BRAVE_API_KEY env > mode-0600
    secret file; generic X-Api-Key / Authorization never repurposed;
    group/world-readable secret file ignored.
  - _brave_search: missing key short-circuits to an error outcome without a
    network call; successful response normalizes the `web.results` array;
    HTTP and timeout errors produce the right state and circuit transitions;
    response size is bounded.
  - _BraveProvider: delegates to _brave_search with the resolved key; timeout
    reads the live module global.
  - provider stack selection: WEBSEARCH_PROVIDER_STACK=brave yields a
    single-provider Brave stack.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
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
    monkeypatch.setattr(srv, "_brave_auth_state", srv._BraveAuthState())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    # Isolate the secret file to a tmp dir so real filesystem state can't leak in.
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", tmp_path / "brave_key")


# --------------------------------------------------------------------------- #
# Normalization.
# --------------------------------------------------------------------------- #

def test_normalize_brave_web_result():
    r = {"url": "https://en.wikipedia.org/wiki/Brave_Search",
         "title": "Brave Search - Wikipedia", "description": "Privacy-focused search."}
    out = srv._normalize_brave_result(r)
    assert out is not None
    assert out["provider"] == "brave"
    assert out["engine"] == "brave"
    assert out["domain"] == "en.wikipedia.org"
    assert out["snippet"] == "Privacy-focused search."


def test_normalize_brave_rejects_private_url():
    assert srv._normalize_brave_result({"url": "http://127.0.0.1/x"}) is None


def test_normalize_brave_preserves_published_hint():
    r = {"url": "https://example.com/a", "title": "A", "description": "body",
         "page_age": "2024-09-30"}
    out = srv._normalize_brave_result(r)
    assert out is not None
    assert "2024-09-30" in out["snippet"]


def test_normalize_brave_missing_url_returns_none():
    assert srv._normalize_brave_result({"title": "no url"}) is None


def test_normalize_brave_empty_description_is_ok():
    r = {"url": "https://example.com/a", "title": "A", "description": ""}
    out = srv._normalize_brave_result(r)
    assert out is not None
    assert out["snippet"] == ""


# --------------------------------------------------------------------------- #
# Credential resolution.
# --------------------------------------------------------------------------- #

def test_brave_auth_state_tracks_interleaved_credentials():
    state = srv._BraveAuthState()

    state.record_auth_failure("credential-a")
    state.record_auth_failure("credential-b")
    assert state.usable("credential-a") is False
    assert state.usable("credential-b") is False

    state.record_success("credential-a")
    assert state.usable("credential-a") is True
    assert state.usable("credential-b") is False


@pytest.mark.asyncio
async def test_resolve_brave_key_env_fallback(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_brave_key() == "brave-env"


@pytest.mark.asyncio
async def test_resolve_brave_key_header_precedence(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-brave-key": "brave-header"})
    assert srv._resolve_brave_key() == "brave-header"


@pytest.mark.asyncio
async def test_resolve_brave_key_does_not_repurpose_generic_api_key(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-api-key": "broker-secret"})
    assert srv._resolve_brave_key() == "brave-env"


@pytest.mark.asyncio
async def test_resolve_brave_key_empty_when_neither(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_brave_key() == ""


@pytest.mark.asyncio
async def test_resolve_brave_key_reads_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "brave_key"
    secret.write_text("brave-from-file\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_brave_key() == "brave-from-file"


@pytest.mark.asyncio
async def test_resolve_brave_key_ignores_world_readable_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "brave_key"
    secret.write_text("brave-insecure\n", encoding="utf-8")
    os.chmod(secret, 0o644)  # group/world readable -> must be ignored
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_brave_key() == ""


# --------------------------------------------------------------------------- #
# _brave_search behavior.
# --------------------------------------------------------------------------- #

class _FakeStreamResponse:
    def __init__(self, data: dict[str, Any], status: int = 200):
        self._data = data
        self.status_code = status
        self.headers = {"content-encoding": "identity"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)

    async def aiter_raw(self):
        yield json.dumps(self._data).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_brave_search_missing_key_short_circuits(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    called = {"network": False}

    class NoNetworkClient:
        async def stream(self, *a, **kw):
            called["network"] = True

    monkeypatch.setattr(srv, "_client", _async_client(NoNetworkClient()))
    outcome = await srv._brave_search("q", 5, "")
    assert outcome.backend == "brave"
    assert outcome.state == "error"
    assert outcome.error == "missing API key"
    assert called["network"] is False


def _async_client(client):
    async def _factory():
        return client
    return _factory




@pytest.mark.asyncio
async def test_brave_search_normalizes_web_results(monkeypatch):
    payload = {
        "type": "search",
        "web": {
            "type": "search",
            "results": [
                {"url": "https://example.com/a", "title": "A", "description": "alpha"},
                {"url": "https://example.com/b", "title": "B", "description": "beta"},
            ],
        },
    }

    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            return _FakeStreamResponse(payload)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.backend == "brave"
    assert outcome.state == "ok"
    assert outcome.credential_mode == "keyed"
    assert [r["url"] for r in outcome.results] == [
        "https://example.com/a", "https://example.com/b"]
    assert all(r["provider"] == "brave" for r in outcome.results)


@pytest.mark.asyncio
async def test_brave_search_empty_results_is_empty_state(monkeypatch):
    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            return _FakeStreamResponse({"web": {"results": []}})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.state == "empty"
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_brave_search_missing_web_block_is_empty_state(monkeypatch):
    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            return _FakeStreamResponse({"type": "search"})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.state == "empty"
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_brave_search_timeout_records_failure(monkeypatch):
    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.state == "timeout"
    assert outcome.error == "request timed out"
    snap = srv._breaker.snapshot("brave")
    assert snap["consecutive_failures"] >= 1
    assert srv._brave_auth_state.usable("brave-key") is True








@pytest.mark.asyncio
async def test_brave_search_429_circuit_breaks(monkeypatch):
    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            return _FakeStreamResponse({}, status=429)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.state == "error"
    assert outcome.http_status == 429
    snap = srv._breaker.snapshot("brave")
    assert snap["consecutive_failures"] >= 1
    assert srv._brave_auth_state.usable("brave-key") is True


@pytest.mark.asyncio
async def test_brave_search_response_is_size_bounded(monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_RESPONSE_MAX_BYTES", 8)

    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            return _FakeStreamResponse({"web": {"results": [
                {"url": "https://x.example/"}]}})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._brave_search("q", 5, "brave-key")
    assert outcome.state == "error"
    assert outcome.error == "ValueError"


@pytest.mark.asyncio
async def test_brave_search_sends_subscription_token_header(monkeypatch):
    seen_headers = {}

    class FakeClient:
        def stream(self, method, url, params=None, headers=None, timeout=None):
            seen_headers.update(headers or {})
            return _FakeStreamResponse({"web": {"results": []}})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    await srv._brave_search("q", 5, "brave-key")
    assert seen_headers.get("X-Subscription-Token") == "brave-key"
    assert seen_headers.get("Accept") == "application/json"


# --------------------------------------------------------------------------- #
# Provider class + stack selection.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_brave_provider_delegates_with_resolved_key(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    seen = {}

    async def fake_brave_search(query, num_results, api_key):
        seen["key"] = api_key
        return srv._BackendOutcome(backend="brave", ok=True, state="ok")

    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    await srv._BraveProvider().search("q", 5)
    assert seen["key"] == "brave-env"
    assert srv._BraveProvider.credential_label() == "keyed"


def test_brave_provider_timeout_reads_module_global(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_TIMEOUT", 0.9)
    assert srv._BraveProvider().timeout == 0.9


def test_provider_stack_brave_opt_in(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    stack = srv._build_provider_stack()
    assert [p.name for p in stack] == ["brave"]


@pytest.mark.asyncio
async def test_health_reports_brave_as_the_only_provider(monkeypatch):
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    health = await srv._health_payload()
    assert health["provider_stack"] == "brave"
    assert [provider["name"] for provider in health["providers"]] == ["brave"]
    assert set(health) == {
        "status", "ready", "service", "policy", "provider_stack",
        "providers", "telemetry", "last_search",
    }
    assert "brave_timeout_s" in health["policy"]





@pytest.mark.asyncio
async def test_brave_stack_web_search_routes_to_brave(monkeypatch):
    """End-to-end: with WEBSEARCH_PROVIDER_STACK=brave, web_search calls Brave."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="brave", ok=True, state="ok", attempts=1,
            results=[{"title": "B", "url": "https://b.example/", "domain": "b.example",
                      "snippet": "b", "engine": "brave", "provider": "brave", "score": None}],
        )
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "brave"
    assert payload["attempted"] == ["brave"]
    assert payload["provider_states"] == {"brave": "ok"}
    assert payload["results"][0]["provider"] == "brave"
    assert set(payload["timings_ms"]) == {"total", "brave"}


@pytest.mark.asyncio
async def test_brave_stack_no_fallback_error_message(monkeypatch):
    """Brave-only stack with a Brave failure reports no fallback configured."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="brave", state="error", error="boom")
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "no fallback provider is configured" in payload["error"]
    assert payload["fallback_reason"] == "brave_error"


# --------------------------------------------------------------------------- #
# Cost estimation.
# --------------------------------------------------------------------------- #

def test_estimate_cost_brave_single_search():
    assert srv._estimate_search_cost(backend="brave", attempted=["brave"]) == 0.005


def test_estimate_cost_no_providers_attempted():
    assert srv._estimate_search_cost(backend="none", attempted=[]) == 0.0




@pytest.mark.asyncio
async def test_brave_search_response_includes_estimated_cost(monkeypatch):
    """The web_search payload includes estimated_cost_usd for a Brave search."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="brave", ok=True, state="ok", attempts=1,
            results=[{"title": "B", "url": "https://b.example/", "domain": "b.example",
                      "snippet": "b", "engine": "brave", "provider": "brave", "score": None}],
        )
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["estimated_cost_usd"] == 0.005


@pytest.mark.asyncio
async def test_error_response_has_zero_cost(monkeypatch):
    """A search that errors before contacting any provider has zero cost."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    result = await _call_tool("web_search", {"query": ""})
    payload = json.loads(_result_text(result))
    assert payload["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_missing_key_skip_is_not_counted_as_attempt_or_cost(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert payload["attempted"] == []
    assert payload["estimated_cost_usd"] == 0.0
