"""Tests for the Kagi Search provider (ADR 0002 Phase 2).

Covers:
  - _normalize_kagi_result: web results (t==0) normalized with provider="kagi";
    related-search entries (t==1) ignored; private/non-http URLs rejected;
    published timestamps preserved as a snippet hint.
  - _resolve_kagi_key: X-Kagi-Key header > KAGI_API_KEY env > mode-0600 secret
    file; generic X-Api-Key / Authorization never repurposed; group/world-readable
    secret file ignored.
  - _kagi_search: missing key short-circuits to an error outcome without a
    network call; successful response normalizes the `data` array; HTTP and
    timeout errors produce the right state and circuit transitions; response
    size is bounded.
  - _KagiProvider: delegates to _kagi_search with the resolved key; timeout
    reads the live module global.
  - provider stack selection: WEBSEARCH_PROVIDER_STACK=kagi yields a
    single-provider Kagi stack; the default remains searxng+tavily.

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
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    # Isolate the secret file to a tmp dir so real filesystem state can't leak in.
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_KAGI_SECRET_FILE", tmp_path / "kagi_key")


# --------------------------------------------------------------------------- #
# Normalization.
# --------------------------------------------------------------------------- #

def test_normalize_kagi_web_result():
    r = {"t": 0, "url": "https://en.wikipedia.org/wiki/Steve_Jobs",
         "title": "Steve Jobs - Wikipedia", "snippet": "Co-founder of Apple."}
    out = srv._normalize_kagi_result(r)
    assert out is not None
    assert out["provider"] == "kagi"
    assert out["engine"] == "kagi"
    assert out["domain"] == "en.wikipedia.org"
    assert out["snippet"] == "Co-founder of Apple."


def test_normalize_kagi_ignores_related_searches():
    r = {"t": 1, "list": ["steve jobs", "steve jobs movie"]}
    assert srv._normalize_kagi_result(r) is None


def test_normalize_kagi_ignores_infobox_entries():
    assert srv._normalize_kagi_result({"t": 2, "url": "https://x.example/"}) is None


def test_normalize_kagi_rejects_private_url():
    assert srv._normalize_kagi_result({"t": 0, "url": "http://127.0.0.1/x"}) is None


def test_normalize_kagi_preserves_published_hint():
    r = {"t": 0, "url": "https://example.com/a", "title": "A", "snippet": "body",
         "published": "2024-09-30T00:00:00Z"}
    out = srv._normalize_kagi_result(r)
    assert out is not None
    assert "2024-09-30T00:00:00Z" in out["snippet"]


def test_normalize_kagi_missing_url_returns_none():
    assert srv._normalize_kagi_result({"t": 0, "title": "no url"}) is None


# --------------------------------------------------------------------------- #
# Credential resolution.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resolve_kagi_key_env_fallback(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_kagi_key() == "kagi-env"


@pytest.mark.asyncio
async def test_resolve_kagi_key_header_precedence(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-kagi-key": "kagi-header"})
    assert srv._resolve_kagi_key() == "kagi-header"


@pytest.mark.asyncio
async def test_resolve_kagi_key_does_not_repurpose_generic_api_key(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-api-key": "broker-secret"})
    assert srv._resolve_kagi_key() == "kagi-env"


@pytest.mark.asyncio
async def test_resolve_kagi_key_empty_when_neither(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_kagi_key() == ""


@pytest.mark.asyncio
async def test_resolve_kagi_key_reads_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "kagi_key"
    secret.write_text("kagi-from-file\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    monkeypatch.setattr(srv, "_KAGI_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_kagi_key() == "kagi-from-file"


@pytest.mark.asyncio
async def test_resolve_kagi_key_ignores_world_readable_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "kagi_key"
    secret.write_text("kagi-insecure\n", encoding="utf-8")
    os.chmod(secret, 0o644)  # group/world readable -> must be ignored
    monkeypatch.setattr(srv, "_KAGI_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_kagi_key() == ""


# --------------------------------------------------------------------------- #
# _kagi_search behavior.
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
async def test_kagi_search_missing_key_short_circuits(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    called = {"network": False}

    class NoNetworkClient:
        async def stream(self, *a, **kw):
            called["network"] = True

    monkeypatch.setattr(srv, "_client", _async_client(NoNetworkClient()))
    outcome = await srv._kagi_search("q", 5, "")
    assert outcome.backend == "kagi"
    assert outcome.state == "error"
    assert outcome.error == "missing API key"
    assert called["network"] is False


def _async_client(client):
    async def _factory():
        return client
    return _factory


@pytest.mark.asyncio
async def test_kagi_search_normalizes_data_array(monkeypatch):
    payload = {"meta": {"api_balance": 1.0}, "data": [
        {"t": 0, "url": "https://example.com/a", "title": "A", "snippet": "alpha"},
        {"t": 1, "list": ["related"]},
        {"t": 0, "url": "https://example.com/b", "title": "B", "snippet": "beta"},
    ]}

    class FakeClient:
        def stream(self, method, url, headers=None, timeout=None):
            return _FakeStreamResponse(payload)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._kagi_search("q", 5, "kagi-key")
    assert outcome.backend == "kagi"
    assert outcome.state == "ok"
    assert outcome.credential_mode == "keyed"
    assert [r["url"] for r in outcome.results] == [
        "https://example.com/a", "https://example.com/b"]
    assert all(r["provider"] == "kagi" for r in outcome.results)


@pytest.mark.asyncio
async def test_kagi_search_empty_data_is_empty_state(monkeypatch):
    class FakeClient:
        def stream(self, method, url, headers=None, timeout=None):
            return _FakeStreamResponse({"data": []})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._kagi_search("q", 5, "kagi-key")
    assert outcome.state == "empty"
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_kagi_search_timeout_records_failure(monkeypatch):
    class FakeClient:
        def stream(self, method, url, headers=None, timeout=None):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._kagi_search("q", 5, "kagi-key")
    assert outcome.state == "timeout"
    assert outcome.error == "request timed out"
    snap = srv._breaker.snapshot("kagi")
    assert snap["consecutive_failures"] >= 1


@pytest.mark.asyncio
async def test_kagi_search_401_does_not_circuit_break(monkeypatch):
    class FakeClient:
        def stream(self, method, url, headers=None, timeout=None):
            return _FakeStreamResponse({}, status=401)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._kagi_search("q", 5, "kagi-key")
    assert outcome.state == "error"
    assert outcome.http_status == 401
    snap = srv._breaker.snapshot("kagi")
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_kagi_search_response_is_size_bounded(monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_RESPONSE_MAX_BYTES", 8)

    class FakeClient:
        def stream(self, method, url, headers=None, timeout=None):
            return _FakeStreamResponse({"data": [{"t": 0, "url": "https://x.example/"}]})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._kagi_search("q", 5, "kagi-key")
    assert outcome.state == "error"
    assert outcome.error == "ValueError"


# --------------------------------------------------------------------------- #
# Provider class + stack selection.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_kagi_provider_delegates_with_resolved_key(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    seen = {}

    async def fake_kagi_search(query, num_results, api_key):
        seen["key"] = api_key
        return srv._BackendOutcome(backend="kagi", ok=True, state="ok")

    monkeypatch.setattr(srv, "_kagi_search", fake_kagi_search)
    await srv._KagiProvider().search("q", 5)
    assert seen["key"] == "kagi-env"
    assert srv._KagiProvider.credential_label() == "keyed"


def test_kagi_provider_timeout_reads_module_global(monkeypatch):
    monkeypatch.setattr(srv, "KAGI_TIMEOUT", 0.9)
    assert srv._KagiProvider().timeout == 0.9


def test_provider_stack_default_is_searxng_plus_tavily():
    names = [p.name for p in srv._PROVIDERS]
    assert names == ["searxng", "tavily"]


def test_provider_stack_kagi_opt_in(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi")
    stack = srv._build_provider_stack()
    assert [p.name for p in stack] == ["kagi"]


def test_provider_stack_searxng_only(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng")
    stack = srv._build_provider_stack()
    assert [p.name for p in stack] == ["searxng"]


@pytest.mark.asyncio
async def test_kagi_stack_web_search_routes_to_kagi(monkeypatch):
    """End-to-end: with WEBSEARCH_PROVIDER_STACK=kagi, web_search calls Kagi."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "kagi-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_kagi_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="kagi", ok=True, state="ok",
            results=[{"title": "K", "url": "https://k.example/", "domain": "k.example",
                      "snippet": "k", "engine": "kagi", "provider": "kagi", "score": None}],
        )
    monkeypatch.setattr(srv, "_kagi_search", fake_kagi_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 1})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "kagi"
    assert payload["attempted"] == ["kagi"]
    assert payload["provider_states"] == {"kagi": "ok"}
    assert payload["results"][0]["provider"] == "kagi"
    assert set(payload["timings_ms"]) == {"total", "kagi"}


@pytest.mark.asyncio
async def test_kagi_stack_no_fallback_error_message(monkeypatch):
    """Kagi-only stack with a Kagi failure reports no fallback configured."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "KAGI_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_kagi_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="kagi", state="error", error="boom")
    monkeypatch.setattr(srv, "_kagi_search", fake_kagi_search)
    result = await _call_tool("web_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert "no fallback provider is configured" in payload["error"]
    assert payload["fallback_reason"] == "kagi_error"
