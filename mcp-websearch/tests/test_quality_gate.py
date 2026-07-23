"""Tests for the search quality gate (ADR 0002 Phase 3).

The gate evaluates the primary provider's deduped candidates in memory and
returns a query-free reason when they are thin, single-domain-dominated, or
duplicate/generic. The gate is enabled only when _QUALITY_GATE_MODE == "on".
With only brave and searxng stacks (no fallback provider), the gate can detect
thin results but has nowhere to fall back to.

Run:  cd mcp-websearch && uv run pytest -q
"""

from __future__ import annotations

from typing import Any

import pytest

import server as srv


def _result(url: str, snippet: str = "s", title: str = "T") -> dict[str, Any]:
    return {"title": title, "url": url, "domain": url.split("/")[2],
            "snippet": snippet, "engine": "brave", "provider": "brave", "score": None}


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

def test_quality_gate_auto_is_disabled(monkeypatch):
    """auto mode no longer enables the gate (no fallback provider exists)."""
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    assert srv._quality_gate_enabled() is False


def test_quality_gate_explicit_on_overrides_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "on")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    assert srv._quality_gate_enabled() is True


def test_quality_gate_explicit_off_overrides_stack(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "off")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "brave")
    assert srv._quality_gate_enabled() is False


def test_quality_gate_searxng_stack_auto_disabled(monkeypatch):
    monkeypatch.setattr(srv, "_QUALITY_GATE_MODE", "auto")
    monkeypatch.setattr(srv, "_PROVIDER_STACK", "searxng")
    assert srv._quality_gate_enabled() is False
