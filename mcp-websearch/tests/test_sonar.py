"""Tests for the Perplexity Sonar provider (ADR 0002 Phase 4).

Covers:
  - _normalize_sonar_result: search_results entries normalized with
    provider="sonar"; private URLs rejected; date/last_updated preserved.
  - _resolve_perplexity_key: X-Perplexity-Key > PERPLEXITY_API_KEY env >
    mode-0600 secret file; generic keys never repurposed; insecure file ignored.
  - _sonar_search: missing key short-circuits; successful response parses the
    generated answer, citations, and normalized search_results; HTTP/timeout
    errors produce the right state and circuit transitions; response bounded.
  - _PerplexityProvider: delegates with the resolved key; output="generated".
  - kagi+sonar stack: quality-gate failure routes to Sonar and surfaces the
    generated answer without merging it into the raw result list.
  - answer_search tool: calls Sonar directly and returns answer + citations.

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


def _async_client(client):
    async def _factory():
        return client
    return _factory


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


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_PERPLEXITY_SECRET_FILE", tmp_path / "perplexity_key")


# --------------------------------------------------------------------------- #
# Normalization.
# --------------------------------------------------------------------------- #

def test_normalize_sonar_result():
    r = {"title": "Q", "url": "https://en.wikipedia.org/wiki/Quantum_computing",
         "snippet": "Quantum computing is...", "date": "2026-05-13", "source": "web"}
    out = srv._normalize_sonar_result(r)
    assert out is not None
    assert out["provider"] == "sonar"
    assert out["engine"] == "sonar"
    assert "2026-05-13" in out["snippet"]


def test_normalize_sonar_result_uses_last_updated():
    r = {"title": "Q", "url": "https://x.example/a", "snippet": "s",
         "last_updated": "2026-05-18"}
    out = srv._normalize_sonar_result(r)
    assert out is not None
    assert "2026-05-18" in out["snippet"]


def test_normalize_sonar_result_rejects_private_url():
    assert srv._normalize_sonar_result({"url": "http://10.0.0.1/x"}) is None


def test_normalize_sonar_result_missing_url_returns_none():
    assert srv._normalize_sonar_result({"title": "no url"}) is None


# --------------------------------------------------------------------------- #
# Credential resolution.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resolve_perplexity_key_env_fallback(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "px-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_perplexity_key() == "px-env"


@pytest.mark.asyncio
async def test_resolve_perplexity_key_header_precedence(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "px-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-perplexity-key": "px-header"})
    assert srv._resolve_perplexity_key() == "px-header"


@pytest.mark.asyncio
async def test_resolve_perplexity_key_reads_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "perplexity_key"
    secret.write_text("px-from-file\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    monkeypatch.setattr(srv, "_PERPLEXITY_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_perplexity_key() == "px-from-file"


@pytest.mark.asyncio
async def test_resolve_perplexity_key_ignores_world_readable_file(monkeypatch, tmp_path):
    secret = tmp_path / "perplexity_key"
    secret.write_text("px-insecure\n", encoding="utf-8")
    os.chmod(secret, 0o644)
    monkeypatch.setattr(srv, "_PERPLEXITY_SECRET_FILE", secret)
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    assert srv._resolve_perplexity_key() == ""


# --------------------------------------------------------------------------- #
# _sonar_search behavior.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sonar_search_missing_key_short_circuits(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    called = {"network": False}

    class NoNetworkClient:
        async def stream(self, *a, **kw):
            called["network"] = True

    monkeypatch.setattr(srv, "_client", _async_client(NoNetworkClient()))
    outcome = await srv._sonar_search("q", 5, "")
    assert outcome.backend == "sonar"
    assert outcome.state == "error"
    assert outcome.error == "missing API key"
    assert called["network"] is False


@pytest.mark.asyncio
async def test_sonar_search_parses_answer_and_results(monkeypatch):
    payload = {
        "model": "sonar",
        "choices": [{"message": {"content": "Quantum computing uses qubits."}}],
        "citations": ["https://en.wikipedia.org/wiki/Quantum_computing"],
        "search_results": [
            {"title": "Q", "url": "https://en.wikipedia.org/wiki/Quantum_computing",
             "snippet": "Quantum computing is...", "date": "2026-05-13"},
            {"title": "A", "url": "https://aws.amazon.com/what-is/quantum-computing/",
             "snippet": "AWS explainer", "last_updated": "2026-05-18"},
        ],
    }

    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse(payload)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._sonar_search("q", 5, "px-key")
    assert outcome.backend == "sonar"
    assert outcome.state == "ok"
    assert outcome.credential_mode == "keyed"
    assert outcome.answer == "Quantum computing uses qubits."
    assert outcome.citations == ["https://en.wikipedia.org/wiki/Quantum_computing"]
    assert len(outcome.results) == 2
    assert all(r["provider"] == "sonar" for r in outcome.results)


@pytest.mark.asyncio
async def test_sonar_search_answer_only_is_ok(monkeypatch):
    payload = {"choices": [{"message": {"content": "An answer."}}], "search_results": []}

    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse(payload)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._sonar_search("q", 5, "px-key")
    assert outcome.state == "ok"
    assert outcome.answer == "An answer."
    assert outcome.results == []


@pytest.mark.asyncio
async def test_sonar_search_timeout_records_failure(monkeypatch):
    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._sonar_search("q", 5, "px-key")
    assert outcome.state == "timeout"
    snap = srv._breaker.snapshot("sonar")
    assert snap["consecutive_failures"] >= 1


@pytest.mark.asyncio
async def test_sonar_search_401_does_not_circuit_break(monkeypatch):
    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse({}, status=401)

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._sonar_search("q", 5, "px-key")
    assert outcome.state == "error"
    assert outcome.http_status == 401
    assert srv._breaker.snapshot("sonar")["state"] == "closed"


@pytest.mark.asyncio
async def test_sonar_search_response_is_size_bounded(monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_RESPONSE_MAX_BYTES", 8)

    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            return _FakeStreamResponse({"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(srv, "_client", _async_client(FakeClient()))
    outcome = await srv._sonar_search("q", 5, "px-key")
    assert outcome.state == "error"
    assert outcome.error == "ValueError"


# --------------------------------------------------------------------------- #
# Provider class + stack.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_perplexity_provider_delegates_with_resolved_key(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "px-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    seen = {}

    async def fake_sonar_search(query, num_results, api_key):
        seen["key"] = api_key
        return srv._BackendOutcome(backend="sonar", ok=True, state="ok", answer="a")
    monkeypatch.setattr(srv, "_sonar_search", fake_sonar_search)
    await srv._PerplexityProvider().search("q", 5)
    assert seen["key"] == "px-env"
    assert srv._PerplexityProvider.output == "generated"


def test_provider_stack_kagi_plus_sonar(monkeypatch):
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi+sonar")
    stack = srv._build_provider_stack()
    assert [p.name for p in stack] == ["kagi", "sonar"]


# --------------------------------------------------------------------------- #
# End-to-end: kagi+sonar stack quality-gate fallback surfaces the answer.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_kagi_sonar_stack_gate_fallback_surfaces_answer(monkeypatch):
    """Kagi returns thin results -> gate fails -> Sonar fallback -> answer surfaced."""
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "kagi+sonar")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")  # on for non-legacy
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 3)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
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
            backend="sonar", ok=True, state="ok",
            answer="Generated answer with citations.",
            citations=["https://s.example/cite"],
            results=[{"title": "S", "url": "https://s.example/a", "domain": "s.example",
                      "snippet": "s", "engine": "sonar", "provider": "sonar", "score": None}],
        )
    monkeypatch.setattr(srv, "_sonar_search", fake_sonar_search)
    result = await _call_tool("web_search", {"query": "q", "num_results": 3})
    payload = json.loads(_result_text(result))
    assert payload["attempted"] == ["kagi", "sonar"]
    assert payload["fallback_reason"] == "quality_below_min_results"
    assert payload["backend"] == "sonar"
    assert payload["answer"] == "Generated answer with citations."
    assert payload["citations"] == ["https://s.example/cite"]
    assert payload["results"][0]["provider"] == "sonar"
    assert set(payload["timings_ms"]) == {"total", "kagi", "sonar"}


# --------------------------------------------------------------------------- #
# answer_search tool.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_answer_search_calls_sonar_directly(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "px-key")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})

    async def fake_sonar_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="sonar", ok=True, state="ok",
            answer="Direct answer.",
            citations=["https://c.example/"],
            results=[{"title": "S", "url": "https://s.example/a", "domain": "s.example",
                      "snippet": "s", "engine": "sonar", "provider": "sonar", "score": None}],
        )
    monkeypatch.setattr(srv, "_sonar_search", fake_sonar_search)
    result = await _call_tool("answer_search", {"query": "what is quantum computing"})
    payload = json.loads(_result_text(result))
    assert payload["backend"] == "sonar"
    assert payload["attempted"] == ["sonar"]
    assert payload["mode"] == "answer"
    assert payload["answer"] == "Direct answer."
    assert payload["citations"] == ["https://c.example/"]
    assert payload["results"][0]["provider"] == "sonar"


@pytest.mark.asyncio
async def test_answer_search_missing_key_errors(monkeypatch):
    monkeypatch.setattr(srv, "PERPLEXITY_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    async def fake_sonar_search(query, num_results, api_key):
        return srv._BackendOutcome(backend="sonar", state="error", error="missing API key")
    monkeypatch.setattr(srv, "_sonar_search", fake_sonar_search)
    result = await _call_tool("answer_search", {"query": "q"})
    payload = json.loads(_result_text(result))
    assert payload["status"] == "error"
    assert payload["mode"] == "answer"
    assert payload["backend"] == "none"
