"""Tests for the search quality gate (ADR 0002 Phase 3).

The gate evaluates the primary provider's deduped candidates in memory and
returns a query-free reason when they are thin, single-domain-dominated, or
duplicate/generic. Auto/on modes escalate only when a fallback exists; shadow
mode evaluates the same decision while returning Brave's response.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import pytest

import server as srv


def _result(
    url: str,
    snippet: str = "s",
    title: str = "T",
    *,
    provider: str = "brave",
) -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "domain": url.split("/")[2],
        "snippet": snippet,
        "engine": provider,
        "provider": provider,
        "score": None,
    }


@pytest.fixture
def dual_runtime(monkeypatch, tmp_path):
    """Run dual-stack searches without touching the process cache or real DB."""
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "_breaker", srv._CircuitBreaker())
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "_last_search", None)
    monkeypatch.setattr(srv, "_get_cache", lambda: None)
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "QUALITY_MIN_RESULTS", 3)
    monkeypatch.setattr(srv, "QUALITY_MIN_DOMAINS", 2)
    monkeypatch.setattr(srv, "QUALITY_DUPLICATE_FRACTION", 0.6)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "brave-env")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    yield telemetry
    telemetry.close()


def _searxng_outcome(
    results: list[dict[str, Any]] | None = None,
    *,
    state: str = "ok",
) -> srv._BackendOutcome:
    return srv._BackendOutcome(
        backend="searxng",
        ok=state in {"ok", "empty", "degraded"},
        state=state,
        results=results or [],
        attempts=1,
    )


def _brave_outcome(results: list[dict[str, Any]] | None = None) -> srv._BackendOutcome:
    return srv._BackendOutcome(
        backend="brave",
        ok=True,
        state="ok" if results else "empty",
        results=results or [],
        attempts=1,
    )


def _passing_searxng_results() -> list[dict[str, Any]]:
    return [
        _result("https://one.example/a", "alpha bravo", provider="searxng"),
        _result("https://two.example/b", "charlie delta", provider="searxng"),
        _result("https://three.example/c", "echo foxtrot", provider="searxng"),
    ]


def _brave_results() -> list[dict[str, Any]]:
    return [
        _result("https://brave-one.example/a", "brave alpha"),
        _result("https://brave-two.example/b", "brave beta"),
        _result("https://brave-three.example/c", "brave gamma"),
    ]


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

def test_quality_gate_auto_requires_fallback(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    assert srv._quality_gate_enabled() is False

    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    assert srv._quality_gate_enabled() is True


def test_quality_gate_explicit_on_still_requires_fallback(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "on")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    assert srv._quality_gate_enabled() is False

    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    assert srv._quality_gate_enabled() is True


def test_quality_gate_explicit_off_overrides_dual_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "off")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng+brave")
    monkeypatch.setattr(srv, "_PROVIDERS", srv._build_provider_stack())
    assert srv._quality_gate_enabled() is False


# --------------------------------------------------------------------------- #
# Dual-stack orchestration and shadow rollout.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dual_stack_quality_pass_uses_only_searxng(dual_runtime, monkeypatch):
    calls: list[str] = []

    async def fake_searxng_search(query, num_results):
        calls.append("searxng")
        return _searxng_outcome(_passing_searxng_results())

    async def fake_brave_search(query, num_results, api_key):
        calls.append("brave")
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("quality pass", 3))

    assert calls == ["searxng"]
    assert payload["backend"] == "searxng"
    assert payload["fallback_reason"] is None
    assert payload["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_dual_stack_empty_searxng_falls_back_to_brave(dual_runtime, monkeypatch):
    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(state="empty")

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("empty primary", 3))

    assert payload["backend"] == "brave"
    assert payload["attempted"] == ["searxng", "brave"]
    assert payload["fallback_reason"] == "searxng_empty"
    assert payload["estimated_cost_usd"] == 0.005


@pytest.mark.asyncio
async def test_dual_stack_searxng_error_falls_back_to_brave(dual_runtime, monkeypatch):
    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(state="error")

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("error primary", 3))

    assert payload["backend"] == "brave"
    assert payload["fallback_reason"] == "searxng_error"
    assert payload["estimated_cost_usd"] == 0.005


@pytest.mark.asyncio
async def test_dual_stack_quality_failure_merges_primary_and_brave(dual_runtime, monkeypatch):
    primary = [_result(
        "https://primary.example/a", "thin primary", provider="searxng"
    )]

    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(primary)

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("thin primary", 3))

    assert payload["fallback_reason"] == "quality_below_min_results"
    assert payload["backend"] == "searxng+brave"
    assert {item["provider"] for item in payload["results"]} == {"searxng", "brave"}
    assert payload["estimated_cost_usd"] == 0.005


@pytest.mark.asyncio
async def test_dual_stack_fallback_respects_total_deadline(dual_runtime, monkeypatch):
    clock = [0.0]
    timeouts: list[tuple[str, float]] = []

    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(state="empty")

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    async def fake_run_backend(backend, awaitable, timeout):
        timeouts.append((backend, timeout))
        outcome = await awaitable
        if backend == "searxng":
            clock[0] = 0.07
        return outcome

    monkeypatch.setattr(srv, "SEARCH_TOTAL_TIMEOUT", 0.1)
    monkeypatch.setattr(srv, "SEARCH_TIMEOUT", 0.07)
    monkeypatch.setattr(srv, "BRAVE_TIMEOUT", 0.08)
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    monkeypatch.setattr(srv, "_run_backend", fake_run_backend)

    await srv._web_search_impl("deadline", 3)
    assert timeouts[0] == ("searxng", 0.07)
    assert timeouts[1][0] == "brave"
    assert timeouts[1][1] == pytest.approx(0.03)
    assert sum(timeout for _, timeout in timeouts) <= 0.100001


@pytest.mark.asyncio
async def test_shadow_mode_returns_brave_and_records_query_free_decision(
    dual_runtime, monkeypatch, caplog
):
    secret_query = "SECRET SHADOW QUERY 7f1b9"

    async def fake_searxng_search(query, num_results):
        return _searxng_outcome([
            _result("https://primary.example/a", "thin", provider="searxng")
        ])

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    with caplog.at_level(logging.INFO, logger="websearch-mcp"):
        payload = json.loads(await srv._web_search_impl(secret_query, 3))

    assert payload["backend"] == "brave"
    assert {item["provider"] for item in payload["results"]} == {"brave"}
    assert payload["attempted"] == ["searxng", "brave"]
    assert payload["estimated_cost_usd"] == 0.005
    assert "would_escalate=True" in caplog.text
    assert "quality_below_min_results" in caplog.text
    assert secret_query not in caplog.text

    assert dual_runtime.flush()
    with sqlite3.connect(dual_runtime.db_path) as conn:
        row = conn.execute(
            "SELECT would_escalate, would_escalate_reason FROM search_events"
        ).fetchone()
        values = "\n".join(
            str(value)
            for table in ("search_events", "provider_events", "engine_failures")
            for db_row in conn.execute(f"SELECT * FROM {table}")
            for value in db_row
        )
    assert row == (1, "quality_below_min_results")
    assert secret_query not in values


@pytest.mark.asyncio
async def test_shadow_mode_records_quality_pass_without_returning_searxng(
    dual_runtime, monkeypatch
):
    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(_passing_searxng_results())

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("shadow pass", 3))

    assert payload["backend"] == "brave"
    assert {item["provider"] for item in payload["results"]} == {"brave"}
    assert dual_runtime.flush()
    with sqlite3.connect(dual_runtime.db_path) as conn:
        row = conn.execute(
            "SELECT would_escalate, would_escalate_reason FROM search_events"
        ).fetchone()
    assert row == (0, None)
