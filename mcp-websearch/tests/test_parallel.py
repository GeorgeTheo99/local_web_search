"""Tests for the Parallel provider, the search route, and the settings switch.

Covers:
  - _normalize_parallel_result / _parallel_request_body: result shape, bounded
    snippets, freshness for current/news intent.
  - _parallel_search: missing key short-circuits; success normalizes results;
    auth failures mark the credential unusable.
  - Route: settings.json default (brave | parallel), Parallel -> Brave
    fallback, providers without a credential skipped, X-Search-Provider
    override on loopback only, per-route cache variants.
  - POST /settings: same-origin JSON only; closed enum; credential required.
  - Sensitive mode never reaches a provider from image or batch search.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import http_server
import server as srv


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    from fastmcp import Client
    async with Client(srv.mcp) as client:
        return await client.call_tool(tool_name, arguments)


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    parts = [getattr(item, "text", None) for item in content or []]
    return "\n".join(p for p in parts if isinstance(p, str))


def _write_secret(path, value: str = "test-key") -> None:
    path.write_text(value + "\n")
    os.chmod(path, 0o600)


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_brave_auth_state", srv._BraveAuthState())
    monkeypatch.setattr(srv, "_parallel_auth_state", srv._BraveAuthState())
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_last_good_settings", None)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_cache", None)
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", tmp_path / "brave_key")
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "_INGRESS", "loopback")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    yield
    telemetry.close()


def _ok(backend: str, url: str) -> srv._BackendOutcome:
    return srv._BackendOutcome(
        backend=backend, ok=True, state="ok", attempts=1,
        results=[{"title": backend, "url": url, "domain": "example.com",
                  "snippet": backend, "engine": backend, "provider": backend, "score": None}],
    )


@pytest.fixture
def providers(monkeypatch):
    """Fake both providers and record which ones were called."""
    calls: list[str] = []
    behavior: dict[str, Any] = {
        "brave": lambda: _ok("brave", "https://example.com/brave"),
        "parallel": lambda: _ok("parallel", "https://example.com/parallel"),
    }

    async def fake_brave(query, num_results, api_key, intent="general"):
        calls.append("brave")
        return behavior["brave"]()

    async def fake_parallel(query, num_results, api_key, intent="general"):
        calls.append("parallel")
        return behavior["parallel"]()

    monkeypatch.setattr(srv, "_brave_search", fake_brave)
    monkeypatch.setattr(srv, "_parallel_search", fake_parallel)
    return calls, behavior


# --------------------------------------------------------------------------- #
# Normalization and request body.
# --------------------------------------------------------------------------- #

def test_normalize_parallel_result_bounds_snippet_and_rejects_private_urls():
    long_excerpt = "word " * 400
    result = srv._normalize_parallel_result({
        "url": "https://example.com/a",
        "title": "A",
        "excerpts": ["", f"Section Title: A\n{long_excerpt}"],
        "publish_date": "2026-11-01",
    })
    assert result["provider"] == "parallel" and result["engine"] == "parallel"
    assert result["domain"] == "example.com"
    assert "\n" not in result["snippet"]
    assert result["snippet"].endswith("(published 2026-11-01)")
    assert len(result["snippet"]) <= srv.PARALLEL_SNIPPET_MAX_CHARS + len(" (published 2026-11-01)")
    assert srv._normalize_parallel_result({"url": "http://127.0.0.1/x", "excerpts": []}) is None
    assert srv._normalize_parallel_result({"url": "ftp://example.com/x"}) is None


def test_parallel_request_body_shape_and_freshness():
    body = srv._parallel_request_body("python release schedule", 10, "general")
    assert body["objective"] == "python release schedule"
    assert body["search_queries"] == ["python release schedule"]
    assert body["mode"] == srv.PARALLEL_MODE
    assert body["advanced_settings"] == {"max_results": 10}
    news = srv._parallel_request_body("q", 4, "news")
    assert set(news["advanced_settings"]["source_policy"]) == {"after_date"}


# --------------------------------------------------------------------------- #
# _parallel_search.
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


def _client_returning(response: _FakeStreamResponse, seen: list[dict[str, Any]]):
    class FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            seen.append({"method": method, "url": url, "json": json, "headers": headers})
            return response

    async def factory():
        return FakeClient()
    return factory


@pytest.mark.asyncio
async def test_parallel_search_missing_key_makes_no_request(monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(srv, "_client", _client_returning(_FakeStreamResponse({}), seen))
    outcome = await srv._parallel_search("q", 5, "")
    assert (outcome.backend, outcome.state, outcome.error) == ("parallel", "error", "missing API key")
    assert outcome.attempts == 0
    assert seen == []


@pytest.mark.asyncio
async def test_parallel_search_normalizes_results(monkeypatch):
    seen: list[dict[str, Any]] = []
    payload = {"search_id": "s", "results": [
        {"url": "https://example.com/a", "title": "A", "excerpts": ["alpha"]},
        {"url": "http://10.0.0.1/private", "title": "P", "excerpts": ["private"]},
        {"url": "https://example.com/b", "title": "B", "excerpts": ["beta"]},
    ]}
    monkeypatch.setattr(srv, "_client", _client_returning(_FakeStreamResponse(payload), seen))
    outcome = await srv._parallel_search("q", 5, "parallel-key")
    assert outcome.state == "ok" and outcome.credential_mode == "keyed"
    assert [r["url"] for r in outcome.results] == ["https://example.com/a", "https://example.com/b"]
    request = seen[0]
    assert request["method"] == "POST" and request["url"] == "https://api.parallel.ai/v1/search"
    assert request["headers"]["x-api-key"] == "parallel-key"
    assert request["json"]["search_queries"] == ["q"]


@pytest.mark.asyncio
async def test_parallel_auth_failure_marks_credential_unusable(monkeypatch, tmp_path):
    _write_secret(tmp_path / "parallel_key", "bad-key")
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(srv, "_client", _client_returning(_FakeStreamResponse({}, 401), seen))
    outcome = await srv._parallel_search("q", 5, "bad-key")
    assert (outcome.state, outcome.http_status) == ("error", 401)
    assert srv._breaker.snapshot("parallel")["state"] == "closed"
    assert srv._provider_credential_configured("parallel") is True
    assert srv._provider_credential_usable("parallel") is False


def test_parallel_key_is_file_only_and_owner_only(tmp_path):
    key = tmp_path / "parallel_key"
    _write_secret(key, "pk")
    assert srv._resolve_parallel_key() == "pk"
    os.chmod(key, 0o644)
    assert srv._resolve_parallel_key() == ""


# --------------------------------------------------------------------------- #
# Settings and route resolution.
# --------------------------------------------------------------------------- #

def test_settings_default_write_and_invalid_file(tmp_path):
    assert srv._read_settings() == {"search_provider": "brave", "source": "default"}
    assert srv._write_settings("parallel") == {"search_provider": "parallel", "source": "settings"}
    path = tmp_path / "settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"search_provider": "parallel"}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".settings-")]
    # An invalid file keeps the last good value instead of flapping.
    path.write_text('{"search_provider": "bing"}')
    assert srv._read_settings()["search_provider"] == "parallel"
    with pytest.raises(ValueError):
        srv._write_settings("bing")


def test_group_writable_settings_file_is_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"search_provider": "parallel"}')
    os.chmod(path, 0o664)
    assert srv._read_settings() == {"search_provider": "brave", "source": "default"}


def test_route_follows_settings_and_skips_unconfigured_parallel(tmp_path):
    assert srv._resolve_search_route().providers == ("brave",)
    srv._write_settings("parallel")
    # No Parallel key: the route quietly stays on Brave.
    assert srv._resolve_search_route().providers == ("brave",)
    _write_secret(tmp_path / "parallel_key")
    route = srv._resolve_search_route()
    assert route.providers == ("parallel", "brave")
    assert route.source == "settings" and route.default_provider == "parallel"


def test_override_header_pins_one_provider_on_loopback_only(monkeypatch, tmp_path):
    _write_secret(tmp_path / "parallel_key")
    srv._write_settings("parallel")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-search-provider": "Brave"})
    route = srv._resolve_search_route()
    assert (route.providers, route.source, route.override) == (("brave",), "override", "brave")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-search-provider": "auto"})
    assert srv._resolve_search_route().providers == ("parallel", "brave")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-search-provider": "bing"})
    ignored = srv._resolve_search_route()
    assert ignored.providers == ("parallel", "brave") and ignored.override_ignored == "unknown_provider"
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-search-provider": "brave"})
    monkeypatch.setattr(srv, "_INGRESS", "tailnet")
    assert srv._resolve_search_route().providers == ("parallel", "brave")


def test_override_to_unconfigured_provider_is_ignored(monkeypatch):
    monkeypatch.setattr(srv, "get_http_headers", lambda: {"x-search-provider": "parallel"})
    route = srv._resolve_search_route()
    assert route.providers == ("brave",)
    assert route.override_ignored == "provider_unconfigured"


def test_cache_variant_differs_per_route():
    brave = srv._search_cache_variant(8, "normal", "general", "brave")
    parallel = srv._search_cache_variant(8, "normal", "general", "parallel+brave")
    pinned = srv._search_cache_variant(8, "normal", "general", "parallel")
    assert len({brave, parallel, pinned}) == 3
    # The Brave-only variant is unchanged, so existing cache entries stay valid.
    assert json.loads(brave)["provider"] == "brave"
    assert json.loads(parallel)["provider"] == f"parallel:{srv.PARALLEL_MODE}+brave"


# --------------------------------------------------------------------------- #
# web_search over the route.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_parallel_route_serves_parallel_results(providers, tmp_path):
    calls, _ = providers
    _write_secret(tmp_path / "parallel_key")
    srv._write_settings("parallel")
    payload = json.loads(_result_text(await _call_tool("web_search", {"query": "q", "num_results": 1})))
    assert calls == ["parallel"]
    assert payload["status"] == "ok" and payload["backend"] == "parallel"
    assert payload["fallback_reason"] is None
    assert payload["route"]["providers"] == ["parallel", "brave"]
    assert set(payload["timings_ms"]) == {"total", "parallel", "brave"}
    assert payload["estimated_cost_usd"] == srv._PROVIDER_COST_USD["parallel"]


@pytest.mark.asyncio
async def test_parallel_failure_falls_back_to_brave(providers, tmp_path):
    calls, behavior = providers
    behavior["parallel"] = lambda: srv._BackendOutcome(
        backend="parallel", state="error", attempts=1, error="HTTP 500", http_status=500
    )
    _write_secret(tmp_path / "parallel_key")
    srv._write_settings("parallel")
    payload = json.loads(_result_text(await _call_tool("web_search", {"query": "q", "num_results": 1})))
    assert calls == ["parallel", "brave"]
    assert payload["status"] == "ok" and payload["backend"] == "brave"
    assert payload["fallback_reason"] == "parallel_error"
    assert payload["attempted"] == ["parallel", "brave"]
    assert payload["provider_states"] == {"parallel": "error", "brave": "ok"}
    stats = srv._telemetry.stats("24h")
    assert set(stats["providers"]) == {"parallel", "brave"}


@pytest.mark.asyncio
async def test_parallel_empty_falls_back_and_both_failing_is_an_error(providers, tmp_path):
    calls, behavior = providers
    behavior["parallel"] = lambda: srv._BackendOutcome(backend="parallel", ok=True, state="empty", attempts=1)
    behavior["brave"] = lambda: srv._BackendOutcome(backend="brave", state="timeout", attempts=1)
    _write_secret(tmp_path / "parallel_key")
    srv._write_settings("parallel")
    payload = json.loads(_result_text(await _call_tool("web_search", {"query": "q", "num_results": 1})))
    assert calls == ["parallel", "brave"]
    assert payload["status"] == "empty"
    assert payload["fallback_reason"] == "parallel_empty"
    behavior["parallel"] = lambda: srv._BackendOutcome(backend="parallel", state="error", attempts=1)
    calls.clear()
    payload = json.loads(_result_text(await _call_tool("web_search", {"query": "q2", "num_results": 1})))
    assert payload["status"] == "error"
    assert payload["error"] == "Search error: all configured providers failed."


@pytest.mark.asyncio
async def test_switching_route_does_not_serve_the_other_routes_cache(providers, tmp_path):
    calls, _ = providers
    _write_secret(tmp_path / "parallel_key")
    first = json.loads(_result_text(await _call_tool("web_search", {"query": "same", "num_results": 1})))
    assert first["backend"] == "brave"
    srv._write_settings("parallel")
    second = json.loads(_result_text(await _call_tool("web_search", {"query": "same", "num_results": 1})))
    assert second["backend"] == "parallel" and second["cache_hit"] is False
    assert calls == ["brave", "parallel"]


# --------------------------------------------------------------------------- #
# Sensitive mode for image and batch search.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_image_search_sensitive_mode_makes_no_external_call(monkeypatch):
    called = {"brave": False}

    async def fake_image(query, num_results, api_key):
        called["brave"] = True
        return srv._BackendOutcome(backend="brave", ok=True, state="ok")

    monkeypatch.setattr(srv, "_brave_image_search", fake_image)
    payload = json.loads(_result_text(await _call_tool("image_search", {"query": "q", "mode": "sensitive"})))
    assert payload["status"] == "error" and payload["mode"] == "sensitive"
    assert called["brave"] is False
    monkeypatch.setattr(srv, "_SEARCH_MODE", "sensitive")
    payload = json.loads(_result_text(await _call_tool("image_search", {"query": "q"})))
    assert payload["status"] == "error" and called["brave"] is False


@pytest.mark.asyncio
async def test_batch_search_honors_sensitive_mode(providers):
    calls, _ = providers
    payload = json.loads(_result_text(await _call_tool(
        "batch_web_search", {"queries": ["a", "b"], "mode": "sensitive"},
    )))
    assert calls == []
    assert payload["mode"] == "sensitive"
    assert payload["status"] == "error"
    assert all(item["mode"] == "sensitive" for item in payload["results"])


# --------------------------------------------------------------------------- #
# POST /settings and the dashboard config.
# --------------------------------------------------------------------------- #

_ORIGIN = "http://127.0.0.1:8889"


def _post(client, body: Any, **headers: str):
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post("/settings", content=raw, headers={"content-type": "application/json", **headers})


def test_settings_post_requires_same_origin_json(tmp_path):
    _write_secret(tmp_path / "parallel_key")
    with TestClient(http_server.build_app(), base_url=_ORIGIN) as client:
        assert _post(client, {"search_provider": "parallel"}).status_code == 403  # no Origin
        assert _post(client, {"search_provider": "parallel"}, origin="http://127.0.0.1:3000").status_code == 403
        assert _post(client, {"search_provider": "parallel"}, origin="http://localhost:8889").status_code == 403
        form = client.post(
            "/settings", content="search_provider=parallel",
            headers={"origin": _ORIGIN, "content-type": "application/x-www-form-urlencoded"},
        )
        assert form.status_code == 403
        assert _post(client, {"search_provider": "bing"}, origin=_ORIGIN).status_code == 400
        assert _post(client, {"search_provider": "brave", "x": 1}, origin=_ORIGIN).status_code == 400
        assert _post(client, "not json", origin=_ORIGIN).status_code == 400
        assert _post(client, "x" * 2000, origin=_ORIGIN).status_code == 413
        assert not (tmp_path / "settings.json").exists()
        ok = _post(client, {"search_provider": "parallel"}, origin=_ORIGIN)
        assert ok.status_code == 200
        assert ok.json()["route"]["providers"] == ["parallel", "brave"]
        config = client.get("/config").json()
    assert config["route"]["default_provider"] == "parallel"
    assert config["route"]["settings_writable"] is True
    assert config["provider_stack"] == "parallel+brave"
    assert config["search"]["parallel"]["credential_configured"] is True


def test_settings_post_refuses_provider_without_credential(tmp_path):
    with TestClient(http_server.build_app(), base_url=_ORIGIN) as client:
        response = _post(client, {"search_provider": "parallel"}, origin=_ORIGIN)
    assert response.status_code == 409
    assert not (tmp_path / "settings.json").exists()


def test_settings_post_refused_on_tailnet_ingress(monkeypatch, tmp_path):
    _write_secret(tmp_path / "parallel_key")
    monkeypatch.setattr(srv, "_INGRESS", "tailnet")
    with TestClient(http_server.build_app(), base_url=_ORIGIN) as client:
        response = _post(client, {"search_provider": "parallel"}, origin=_ORIGIN)
    assert response.status_code == 403
    host = "device.tailnet.ts.net"
    with TestClient(http_server.build_app(tailnet_host=host), base_url=f"https://{host}") as client:
        blocked = _post(client, {"search_provider": "parallel"}, origin=f"https://{host}")
    assert blocked.status_code in {403, 404}
    assert not (tmp_path / "settings.json").exists()
