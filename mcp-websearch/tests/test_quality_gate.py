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
    engine: str | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "domain": url.split("/")[2],
        "snippet": snippet,
        "engine": engine or provider,
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
    unresponsive_engines: list[Any] | None = None,
) -> srv._BackendOutcome:
    return srv._BackendOutcome(
        backend="searxng",
        ok=state in {"ok", "empty", "degraded"},
        state=state,
        results=results or [],
        attempts=1,
        unresponsive_engines=unresponsive_engines or [],
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


def test_quality_gate_fails_when_bing_is_unavailable():
    passed, reason = srv._quality_gate(
        _passing_searxng_results(),
        unresponsive_engines=[["bing", "timeout for SECRET QUERY"]],
    )
    assert passed is False
    assert reason == "quality_critical_engine_unavailable"


def test_quality_gate_ignores_noncritical_engine_failures():
    passed, reason = srv._quality_gate(
        _passing_searxng_results(),
        unresponsive_engines=[["mwmbl", "timeout"]],
    )
    assert passed is True
    assert reason is None


def test_shadow_parity_uses_deduped_top_k_and_numeric_overlap_only():
    primary = srv._BackendOutcome(
        backend="searxng",
        ok=True,
        state="ok",
        results=[
            _result(
                "https://top.example/a?utm_source=private",
                provider="searxng",
                engine="bing",
            ),
            _result("https://top.example/a", provider="searxng", engine="bing"),
            _result("https://top.example/b", provider="searxng", engine="wikipedia"),
            _result("https://other.example/c", provider="searxng", engine="mwmbl"),
            _result("https://ignored.example/d", provider="searxng", engine="github"),
        ],
    )
    reference = srv._BackendOutcome(
        backend="brave",
        ok=True,
        state="ok",
        results=[
            _result("https://top.example/z"),
            _result("https://top.example/a"),
            _result("https://ref.example/q"),
            _result("https://other.example/x"),
        ],
    )

    parity = srv._compute_shadow_parity(primary, reference, requested=4)

    assert parity == srv.ShadowParity(
        top_k=4,
        primary_result_count=4,
        reference_result_count=4,
        canonical_url_overlap_count=1,
        primary_distinct_domain_count=3,
        reference_distinct_domain_count=3,
        domain_overlap_count=2,
        reference_top_domain_in_primary=1,
        primary_general_web_result_count=2,
    )
    assert all(isinstance(value, int) for value in parity.__dict__.values())


def test_shadow_parity_canonicalizes_default_ports_trailing_hosts_and_idna():
    primary = _searxng_outcome([
        _result(
            "HTTPS://BÜCHER.Example.:443/Case?keep=One&utm_source=private",
            provider="searxng",
            engine="bing",
        )
    ])
    reference = _brave_outcome([
        _result("https://xn--bcher-kva.example/Case?keep=One#fragment")
    ])

    parity = srv._compute_shadow_parity(primary, reference, requested=1)

    assert parity == srv.ShadowParity(
        top_k=1,
        primary_result_count=1,
        reference_result_count=1,
        canonical_url_overlap_count=1,
        primary_distinct_domain_count=1,
        reference_distinct_domain_count=1,
        domain_overlap_count=1,
        reference_top_domain_in_primary=1,
        primary_general_web_result_count=1,
    )


def test_shadow_parity_includes_legitimate_empty_but_rejects_failures():
    empty_primary = _searxng_outcome(state="empty")
    empty_reference = _brave_outcome()
    assert srv._compute_shadow_parity(empty_primary, empty_reference, 8) == srv.ShadowParity(
        top_k=5,
        primary_result_count=0,
        reference_result_count=0,
        canonical_url_overlap_count=0,
        primary_distinct_domain_count=0,
        reference_distinct_domain_count=0,
        domain_overlap_count=0,
        reference_top_domain_in_primary=0,
        primary_general_web_result_count=0,
    )
    failed = srv._BackendOutcome(backend="brave", ok=False, state="timeout")
    assert srv._compute_shadow_parity(empty_primary, failed, 3) is None


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
async def test_dual_stack_bing_failure_escalates_with_query_free_reason(
    dual_runtime, monkeypatch
):
    secret_query = "SECRET BING FAILURE QUERY 91d2"
    calls: list[str] = []

    async def fake_searxng_search(query, num_results):
        calls.append("searxng")
        return _searxng_outcome(
            _passing_searxng_results(),
            state="degraded",
            unresponsive_engines=[["bing", "timeout for SECRET QUERY"]],
        )

    async def fake_brave_search(query, num_results, api_key):
        calls.append("brave")
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl(secret_query, 3))

    assert calls == ["searxng", "brave"]
    assert payload["fallback_reason"] == "quality_critical_engine_unavailable"
    assert dual_runtime.flush()
    with sqlite3.connect(dual_runtime.db_path) as conn:
        reason = conn.execute(
            "SELECT fallback_reason FROM search_events"
        ).fetchone()[0]
    assert reason == "quality_critical_engine_unavailable"
    assert secret_query.encode() not in dual_runtime.db_path.read_bytes()


@pytest.mark.asyncio
async def test_dual_stack_noncritical_engine_failure_does_not_escalate(
    dual_runtime, monkeypatch
):
    calls: list[str] = []

    async def fake_searxng_search(query, num_results):
        calls.append("searxng")
        return _searxng_outcome(
            _passing_searxng_results(),
            state="degraded",
            unresponsive_engines=[["mwmbl", "timeout"]],
        )

    async def fake_brave_search(query, num_results, api_key):
        calls.append("brave")
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("noncritical failure", 3))

    assert calls == ["searxng"]
    assert payload["status"] == "degraded"
    assert payload["fallback_reason"] is None


@pytest.mark.asyncio
async def test_empty_searxng_with_engine_failures_is_labeled_empty(
    dual_runtime, monkeypatch
):
    async def fake_searxng_request(path, params, timeout=None):
        return {
            "results": [],
            "suggestions": [],
            "unresponsive_engines": [["bing", "timeout"]],
        }

    monkeypatch.setattr(srv, "_searxng_request", fake_searxng_request)
    outcome = await srv._searxng_search("empty", 3)
    assert outcome.state == "empty"

    async def fake_searxng_search(query, num_results):
        return outcome

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("empty", 3))
    assert payload["fallback_reason"] == "searxng_empty"


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
        parity_row = conn.execute(
            "SELECT top_k, primary_result_count, reference_result_count, "
            "canonical_url_overlap_count, primary_distinct_domain_count, "
            "reference_distinct_domain_count, domain_overlap_count, "
            "reference_top_domain_in_primary, primary_general_web_result_count "
            "FROM shadow_parity"
        ).fetchone()
        values = "\n".join(
            str(value)
            for table in (
                "search_events",
                "provider_events",
                "engine_failures",
                "shadow_parity",
            )
            for db_row in conn.execute(f"SELECT * FROM {table}")
            for value in db_row
        )
    assert row == (1, "quality_below_min_results")
    assert parity_row == (3, 1, 3, 0, 1, 3, 0, 0, 0)
    assert secret_query not in values
    assert "primary.example" not in values
    assert "brave-one.example" not in values


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("brave_state", "brave_ok", "attempts", "expected_reason"),
    [
        ("error", False, 1, "brave_error"),
        ("timeout", False, 1, "brave_timeout"),
        ("circuit_open", False, 0, "brave_circuit_open"),
        ("empty", True, 1, "brave_empty"),
    ],
)
async def test_shadow_mode_uses_searxng_when_brave_is_unusable(
    dual_runtime,
    monkeypatch,
    brave_state,
    brave_ok,
    attempts,
    expected_reason,
):
    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(_passing_searxng_results())

    async def fake_brave_search(query, num_results, api_key):
        return srv._BackendOutcome(
            backend="brave",
            ok=brave_ok,
            state=brave_state,
            attempts=attempts,
        )

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("shadow fallback", 3))

    assert payload["status"] == "degraded"
    assert payload["backend"] == "searxng"
    assert payload["fallback_reason"] == expected_reason
    assert {result["provider"] for result in payload["results"]} == {"searxng"}
    assert payload["provider_states"]["brave"] == brave_state
    assert dual_runtime.flush()
    with sqlite3.connect(dual_runtime.db_path) as conn:
        parity_count = conn.execute("SELECT COUNT(*) FROM shadow_parity").fetchone()[0]
    assert parity_count == int(brave_ok)


@pytest.mark.asyncio
async def test_shadow_emergency_response_is_not_cached_and_brave_is_retried(
    dual_runtime, monkeypatch, tmp_path
):
    cache = srv.WebCache(tmp_path / "real-cache")
    brave_calls = 0

    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(_passing_searxng_results())

    async def recovering_brave_search(query, num_results, api_key):
        nonlocal brave_calls
        brave_calls += 1
        if brave_calls == 1:
            return srv._BackendOutcome(
                backend="brave", ok=False, state="error", attempts=1
            )
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_get_cache", lambda: cache)
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", recovering_brave_search)
    try:
        first = json.loads(await srv._web_search_impl("shadow recovery", 3))
        second = json.loads(await srv._web_search_impl("shadow recovery", 3))
    finally:
        cache.close()

    assert first["backend"] == "searxng"
    assert first["cache_hit"] is False
    assert second["backend"] == "brave"
    assert second["cache_hit"] is False
    assert brave_calls == 2


@pytest.mark.asyncio
async def test_shadow_usable_brave_response_remains_cacheable(
    dual_runtime, monkeypatch, tmp_path
):
    cache = srv.WebCache(tmp_path / "real-cache")
    calls = {"searxng": 0, "brave": 0}

    async def fake_searxng_search(query, num_results):
        calls["searxng"] += 1
        return _searxng_outcome(_passing_searxng_results())

    async def fake_brave_search(query, num_results, api_key):
        calls["brave"] += 1
        return _brave_outcome(_brave_results())

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_get_cache", lambda: cache)
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    try:
        first = json.loads(await srv._web_search_impl("shadow cached", 3))
        second = json.loads(await srv._web_search_impl("shadow cached", 3))
    finally:
        cache.close()

    assert first["backend"] == "brave"
    assert first["cache_hit"] is False
    assert second["backend"] == "brave"
    assert second["cache_hit"] is True
    assert calls == {"searxng": 1, "brave": 1}


@pytest.mark.asyncio
async def test_shadow_mode_records_parity_for_two_legitimate_empty_outcomes(
    dual_runtime, monkeypatch
):
    async def fake_searxng_search(query, num_results):
        return _searxng_outcome(state="empty")

    async def fake_brave_search(query, num_results, api_key):
        return _brave_outcome()

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_searxng_search", fake_searxng_search)
    monkeypatch.setattr(srv, "_brave_search", fake_brave_search)
    payload = json.loads(await srv._web_search_impl("both empty", 8))

    assert payload["status"] == "empty"
    assert dual_runtime.flush()
    with sqlite3.connect(dual_runtime.db_path) as conn:
        row = conn.execute(
            "SELECT top_k, primary_result_count, reference_result_count "
            "FROM shadow_parity"
        ).fetchone()
    assert row == (5, 0, 0)


@pytest.mark.asyncio
async def test_shadow_readiness_requires_brave_usability(dual_runtime, monkeypatch):
    async def reachable():
        return {"reachable": True, "latency_ms": 1.0}

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_probe_searxng", reachable)
    monkeypatch.setattr(srv, "BRAVE_API_KEY_ENV", "")
    monkeypatch.setattr(srv, "_BRAVE_SECRET_FILE", srv.LOCAL_SEARCH_DATA_DIR / "missing")
    monkeypatch.setattr(srv, "get_http_headers", lambda: {})
    health = await srv._health_payload()

    assert health["searxng"]["available"] is True
    assert health["ready"] is False


@pytest.mark.asyncio
async def test_shadow_readiness_requires_searxng_usability(dual_runtime, monkeypatch):
    async def unavailable():
        return {"reachable": False, "latency_ms": 1.0}

    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "shadow")
    monkeypatch.setattr(srv, "_probe_searxng", unavailable)
    health = await srv._health_payload()

    assert health["searxng"]["available"] is False
    assert health["providers"][1]["credential_configured"] is True
    assert health["ready"] is False
